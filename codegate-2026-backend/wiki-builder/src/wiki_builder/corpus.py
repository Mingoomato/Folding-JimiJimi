from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from wiki_builder.config import WikiConfig
from wiki_builder.errors import ValidationError
from wiki_builder.models import BuildData, Document, Link, Section
from wiki_builder.provenance import compatible_model_fingerprint, model_display_name
from wiki_builder.schemas import validate_record

MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
SEMANTIC_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]{2,}")
SEMANTIC_STOP_WORDS = frozenset(
    {"문서", "파일", "내용", "업무", "보고서", "작성", "관련", "대한", "위한", "그리고"}
)
SEMANTIC_MIN_SHARED_TOKENS = 4
SEMANTIC_MIN_CONTAINMENT = 0.35
SEMANTIC_MAX_LINKS_PER_DOCUMENT = 3


def normalize_alias(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def split_section_text(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    paragraphs = re.split(r"\n{2,}", text)
    parts: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            parts.append(current)
            overlap = current[-overlap_chars:] if overlap_chars else ""
            current = f"{overlap}\n\n{paragraph}" if overlap else paragraph
        else:
            cursor = 0
            while cursor < len(paragraph):
                end = min(cursor + max_chars, len(paragraph))
                parts.append(paragraph[cursor:end])
                if end == len(paragraph):
                    current = ""
                    break
                cursor = max(end - overlap_chars, cursor + 1)
        while len(current) > max_chars:
            parts.append(current[:max_chars])
            current = current[max_chars - overlap_chars :]
    if current:
        parts.append(current)
    return [part.strip() for part in parts if part.strip()]


def build_chunks(config: WikiConfig, documents: Iterable[Document]) -> list[dict[str, Any]]:
    max_chars = int(config.raw["chunking"]["max_chars"])
    overlap_chars = int(config.raw["chunking"]["overlap_chars"])
    records: list[dict[str, Any]] = []
    for document in documents:
        ordinal = 0
        for section in document.sections:
            parts = (
                [section.text]
                if section.pre_chunked
                else split_section_text(section.text, max_chars, overlap_chars)
            )
            for part_number, text in enumerate(parts, start=1):
                ordinal += 1
                if section.pre_chunked:
                    chunk_type = "source-fragment"
                else:
                    chunk_type = "section" if len(parts) == 1 else "section-part"
                chunk_id = (
                    f"{document.doc_id}@{document.revision}#{section.section_id}:c{part_number:03d}"
                )
                embedding_text = f"{document.title}\n{section.heading}\n{text}"
                record = {
                    "schema_version": config.schema_version,
                    "wiki_version": config.wiki_version,
                    "chunk_id": chunk_id,
                    "doc_id": document.doc_id,
                    "revision": document.revision,
                    "ordinal": ordinal,
                    "section_id": section.section_id,
                    "heading_path": list(section.heading_path),
                    "chunk_type": chunk_type,
                    "text": text,
                    "embedding_text": embedding_text,
                    "char_count": len(text),
                    "source_path": document.output_relative_path,
                    "source_chunk_no": section.source_chunk_no,
                    "source_input_path": (f"source-md/{section.source_input_relative_path}"),
                    "source_input_sha256": section.source_input_sha256,
                    "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "status": document.metadata["status"],
                    "effective_from": document.metadata["effective_from"],
                    "effective_to": document.metadata["effective_to"],
                    "tags": list(document.metadata["tags"]),
                    "access": document.metadata["access"],
                }
                validate_record(config, "chunk", record, chunk_id)
                records.append(record)
    return records


def build_aliases(config: WikiConfig, documents: Iterable[Document]) -> dict[str, Any]:
    targets: dict[str, list[dict[str, str]]] = defaultdict(list)
    for document in documents:
        for declared in document.metadata["aliases"]:
            term = normalize_alias(str(declared))
            target = {"doc_id": document.doc_id, "kind": "declared"}
            if target not in targets[term]:
                targets[term].append(target)
    entries = [
        {"term": term, "targets": sorted(items, key=lambda item: item["doc_id"])}
        for term, items in sorted(targets.items(), key=lambda item: item[0])
    ]
    value = {
        "schema_version": config.schema_version,
        "normalization": "NFC+whitespace",
        "entries": entries,
    }
    validate_record(config, "aliases", value, "aliases.json")
    return value


def _link_evidence_quote(section: Section, target: str) -> str:
    for line in section.text.splitlines():
        if f"]({target})" in line:
            return line.strip()
    raise ValidationError(f"링크 근거 행을 찾지 못했습니다: {target}")


def extract_links(documents: Iterable[Document]) -> list[Link]:
    document_list = list(documents)
    known = {document.doc_id for document in document_list}
    links: list[Link] = []
    for document in document_list:
        for section in document.sections:
            for match in MARKDOWN_LINK_RE.finditer(section.text):
                target = match.group(1).split("#", maxsplit=1)[0]
                if "://" in target or target.startswith(("mailto:", "#")):
                    continue
                if not target.lower().endswith(".md"):
                    continue
                to_doc_id = Path(target).stem
                if to_doc_id not in known:
                    raise ValidationError(
                        f"{document.doc_id} {section.section_id}: 깨진 내부 링크 {target}"
                    )
                links.append(
                    Link(
                        from_doc_id=document.doc_id,
                        from_section_id=section.section_id,
                        to_doc_id=to_doc_id,
                        evidence_quote=_link_evidence_quote(section, match.group(1)),
                    )
                )
    return links


def _semantic_tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in SEMANTIC_TOKEN_RE.findall(unicodedata.normalize("NFC", text))
        if token.lower() not in SEMANTIC_STOP_WORDS
    }


def _semantic_evidence(document: Document, shared: set[str]) -> tuple[str, str] | None:
    ranked = sorted(
        document.sections,
        key=lambda section: len(_semantic_tokens(section.evidence_text) & shared),
        reverse=True,
    )
    for section in ranked:
        for line in section.evidence_text.splitlines():
            quote = line.strip()
            if quote and _semantic_tokens(quote) & shared:
                return section.section_id, quote[:500]
    return None


def discover_links(config: WikiConfig, documents: Iterable[Document]) -> list[Link]:
    """Combine explicit Markdown links with bounded local content-similarity candidates."""
    document_list = list(documents)
    explicit = extract_links(document_list)
    synthetic_corpus = bool(
        config.scope.get(
            "synthetic_corpus",
            config.raw.get("defaults", {}).get("synthetic_corpus", False),
        )
    )
    if synthetic_corpus or len(document_list) < 2:
        return explicit

    tokens_by_id = {
        document.doc_id: _semantic_tokens(document.normalized_body)
        for document in document_list
    }
    postings: dict[str, set[str]] = defaultdict(set)
    for document_id, tokens in tokens_by_id.items():
        for token in tokens:
            postings[token].add(document_id)
    explicit_targets = {(link.from_doc_id, link.to_doc_id) for link in explicit}
    semantic: list[Link] = []
    for document in document_list:
        source_tokens = tokens_by_id[document.doc_id]
        overlap_counts: Counter[str] = Counter()
        for token in source_tokens:
            overlap_counts.update(postings[token] - {document.doc_id})
        ranked: list[tuple[float, int, str, set[str]]] = []
        for target_id, shared_count in overlap_counts.items():
            if shared_count < SEMANTIC_MIN_SHARED_TOKENS:
                continue
            target_tokens = tokens_by_id[target_id]
            denominator = min(len(source_tokens), len(target_tokens))
            if denominator == 0:
                continue
            containment = shared_count / denominator
            if containment < SEMANTIC_MIN_CONTAINMENT:
                continue
            shared = source_tokens & target_tokens
            ranked.append((containment, shared_count, target_id, shared))
        ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
        added = 0
        for _score, _shared_count, target_id, shared in ranked:
            if (document.doc_id, target_id) in explicit_targets:
                continue
            evidence = _semantic_evidence(document, shared)
            if evidence is None:
                continue
            section_id, quote = evidence
            semantic.append(
                Link(
                    from_doc_id=document.doc_id,
                    from_section_id=section_id,
                    to_doc_id=target_id,
                    evidence_quote=quote,
                    origin="semantic",
                )
            )
            added += 1
            if added >= SEMANTIC_MAX_LINKS_PER_DOCUMENT:
                break
    return [*explicit, *semantic]


def build_links(
    config: WikiConfig,
    links: Iterable[Link],
    enrichments: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for link in links:
        relation_type = link.relation_type
        origin = link.origin
        enrichment = enrichments.get(link.from_doc_id)
        if enrichment and link.origin == "explicit":
            relation = next(
                (item for item in enrichment["relations"] if item["to_doc_id"] == link.to_doc_id),
                None,
            )
            if relation and relation["relation_type"] != "references":
                relation_type = relation["relation_type"]
                origin = "llm-classified"
        record = {
            "schema_version": config.schema_version,
            "wiki_version": config.wiki_version,
            "from_doc_id": link.from_doc_id,
            "from_section_id": link.from_section_id,
            "to_doc_id": link.to_doc_id,
            "relation_type": relation_type,
            "origin": origin,
            "evidence_quote": link.evidence_quote,
        }
        validate_record(
            config,
            "link",
            record,
            f"{link.from_doc_id}->{link.to_doc_id}",
        )
        records.append(record)
    return sorted(
        records,
        key=lambda item: (
            item["from_doc_id"],
            item["from_section_id"],
            item["to_doc_id"],
        ),
    )


def current_enrichments(
    config: WikiConfig, documents: Iterable[Document]
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    approved_dir = config.resolve_path("enrichment_dir") / "approved"
    approved: dict[str, dict[str, Any]] = {}
    status: dict[str, str] = {}
    for document in documents:
        path = approved_dir / f"{document.doc_id}.json"
        if not path.is_file():
            status[document.doc_id] = "pending"
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            validate_record(config, "enrichment", record, document.doc_id)
        except (json.JSONDecodeError, ValidationError):
            status[document.doc_id] = "stale"
            continue
        is_current = all(
            (
                record["source_sha256"] == document.original_sha256,
                record["prompt_version"] == config.prompt_version,
                compatible_model_fingerprint(config, record["model_fingerprint"]),
            )
        )
        if is_current:
            approved[document.doc_id] = record
            status[document.doc_id] = "approved"
        else:
            status[document.doc_id] = "stale"
    needs_review_dir = config.resolve_path("enrichment_dir") / "needs-review"
    for document in documents:
        review_path = needs_review_dir / f"{document.doc_id}.json"
        if review_path.is_file() and status.get(document.doc_id) != "approved":
            try:
                review = json.loads(review_path.read_text(encoding="utf-8"))
                if review.get("source_sha256") == document.original_sha256:
                    status[document.doc_id] = "needs-review"
            except json.JSONDecodeError:
                status[document.doc_id] = "needs-review"
    return approved, status


def build_manifest(
    config: WikiConfig,
    documents: Iterable[Document],
    chunks: Iterable[dict[str, Any]],
    links: Iterable[dict[str, Any]],
    enrichments: dict[str, dict[str, Any]],
    enrichment_status: dict[str, str],
) -> list[dict[str, Any]]:
    chunk_counts = Counter(chunk["doc_id"] for chunk in chunks)
    link_counts = Counter(link["from_doc_id"] for link in links)
    display_model = model_display_name(config)
    records: list[dict[str, Any]] = []
    for document in documents:
        enrichment = enrichments.get(document.doc_id)
        output_bytes = document.normalized_markdown.encode("utf-8")
        status = enrichment_status[document.doc_id]
        record = {
            "schema_version": config.schema_version,
            "wiki_version": config.wiki_version,
            "doc_id": document.doc_id,
            "revision": document.revision,
            "title": document.title,
            "doc_type": document.metadata["doc_type"],
            "language": document.metadata["language"],
            "status": document.metadata["status"],
            "official_number": document.metadata["official_number"],
            "authority_level": document.metadata["authority_level"],
            "issuing_org": document.metadata["issuing_org"],
            "issued_on": document.metadata["issued_on"],
            "effective_from": document.metadata["effective_from"],
            "effective_to": document.metadata["effective_to"],
            "path": document.output_relative_path,
            "access": document.metadata["access"],
            "tags": list(document.metadata["tags"]),
            "aliases": list(document.metadata["aliases"]),
            "source": dict(document.metadata["source"]),
            "ingest": {
                "aggregate_sha256": document.original_sha256,
                "fragments": [
                    {
                        "chunk_no": fragment.chunk_no,
                        "path": f"source-md/{fragment.input_relative_path}",
                        "sha256": fragment.original_sha256,
                    }
                    for fragment in document.source_fragments
                ],
            },
            "source_fragment_count": len(document.source_fragments),
            "chunking_mode": document.chunking_mode,
            "content_sha256": hashlib.sha256(output_bytes).hexdigest(),
            "section_count": len(document.sections),
            "chunk_count": chunk_counts[document.doc_id],
            "link_count": link_counts[document.doc_id],
            "summary": enrichment["summary"]["text"] if enrichment else None,
            "keywords": list(enrichment["keywords"]) if enrichment else [],
            "enrichment": {
                "status": status,
                "model": display_model if enrichment else None,
                "prompt_version": config.prompt_version if enrichment else None,
                "record_ref": (
                    f"retrieval/enrichments.jsonl#{document.doc_id}" if enrichment else None
                ),
            },
        }
        validate_record(config, "manifest", record, document.doc_id)
        records.append(record)
    return sorted(records, key=lambda item: item["doc_id"])


def assemble_build_data(config: WikiConfig, documents: list[Document]) -> BuildData:
    approved, statuses = current_enrichments(config, documents)
    discovered_links = discover_links(config, documents)
    chunks = build_chunks(config, documents)
    aliases = build_aliases(config, documents)
    links = build_links(config, discovered_links, approved)
    manifest = build_manifest(config, documents, chunks, links, approved, statuses)
    return BuildData(
        documents=documents,
        chunks=chunks,
        aliases=aliases,
        links=links,
        manifest=manifest,
        enrichments=[approved[key] for key in sorted(approved)],
    )


def verify_expected_corpus(expected: dict[str, Any], data: BuildData) -> None:
    actual_type_counts = Counter(doc.metadata["doc_type"] for doc in data.documents)
    actual_status_counts = Counter(doc.metadata["status"] for doc in data.documents)
    checks = {
        "documents": (len(data.documents), int(expected["documents"])),
        "chunks": (len(data.chunks), int(expected["chunks"])),
        "aliases": (len(data.aliases["entries"]), int(expected["aliases"])),
        "explicit_links": (len(extract_links(data.documents)), int(expected["explicit_links"])),
    }
    failures = [
        f"{name}: 실제 {actual}, 예상 {wanted}"
        for name, (actual, wanted) in checks.items()
        if actual != wanted
    ]
    for name, wanted in expected["doc_types"].items():
        if actual_type_counts[name] != wanted:
            failures.append(f"doc_type {name}: 실제 {actual_type_counts[name]}, 예상 {wanted}")
    for name, wanted in expected["statuses"].items():
        if actual_status_counts[name] != wanted:
            failures.append(f"status {name}: 실제 {actual_status_counts[name]}, 예상 {wanted}")
    if failures:
        raise ValidationError("코퍼스 합격 기준 불일치\n- " + "\n- ".join(failures))
