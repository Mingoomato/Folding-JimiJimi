from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator

from wiki_builder.config import WikiConfig, WikiScope, scoped_config
from wiki_builder.corpus import extract_links
from wiki_builder.errors import ProviderError, ValidationError
from wiki_builder.gemini import GeminiClient, get_api_key
from wiki_builder.io_utils import atomic_write_json
from wiki_builder.markdown import load_documents
from wiki_builder.models import Document, Link
from wiki_builder.provenance import compatible_model_fingerprint, model_fingerprint
from wiki_builder.schemas import load_schema, validate_record

NUMBER_RE = re.compile(r"(?<![0-9A-Za-z])[0-9]+(?:[.,][0-9]+)*(?![0-9A-Za-z])")
SENTENCE_END_RE = re.compile(r"[.!?][\"'”’)]?(?:\s|$)")
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
FORBIDDEN_OUTPUT = ("```", "<think>", "</think>", "analysis:", "사고 과정")


@dataclass
class EnrichmentRun:
    processed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def model_response_schema(config: WikiConfig) -> dict[str, Any]:
    full = load_schema(config, "enrichment")
    kept = ("doc_id", "summary", "key_points", "keywords", "entities", "faq", "relations")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": list(kept),
        "properties": {key: full["properties"][key] for key in kept},
        "$defs": full["$defs"],
    }


def _document_source_text(document: Document) -> str:
    metadata = {key: value for key, value in document.metadata.items() if key not in {"source"}}
    return unicodedata.normalize(
        "NFC",
        json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n" + document.normalized_body,
    )


def _iter_evidence(record: dict[str, Any]) -> Iterable[dict[str, str]]:
    yield from record["summary"]["evidence"]
    for item in record["key_points"]:
        yield from item["evidence"]
    for item in record["entities"]:
        yield from item["evidence"]
    for item in record["faq"]:
        yield from item["evidence"]
    for item in record["relations"]:
        yield from item["evidence"]


def _generated_texts(record: dict[str, Any]) -> Iterable[str]:
    yield record["summary"]["text"]
    for item in record["key_points"]:
        yield item["text"]
    for item in record["faq"]:
        yield item["question"]
        yield item["answer"]


def _exact_section_evidence(section_text: dict[str, str], evidence: object) -> bool:
    if not isinstance(evidence, dict):
        return False
    section_id = evidence.get("section_id")
    quote = evidence.get("quote")
    return (
        isinstance(section_id, str)
        and isinstance(quote, str)
        and section_id in section_text
        and quote in section_text[section_id]
    )


def sanitize_optional_grounding(document: Document, record: dict[str, Any]) -> dict[str, Any]:
    """Remove unsupported optional model suggestions without changing required prose."""
    cleaned = deepcopy(record)
    source_text = _document_source_text(document)

    keywords = cleaned.get("keywords")
    if isinstance(keywords, list):
        grounded_keywords = [
            keyword
            for keyword in keywords
            if isinstance(keyword, str) and unicodedata.normalize("NFC", keyword) in source_text
        ]
        # Keep schema-invalid output intact when filtering would leave too few items,
        # so the normal retry path can ask the provider to repair it.
        if len(grounded_keywords) >= 5:
            cleaned["keywords"] = grounded_keywords

    entities = cleaned.get("entities")
    if isinstance(entities, list):
        section_text = {section.section_id: section.evidence_text for section in document.sections}
        grounded_entities: list[dict[str, Any]] = []
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            name = entity.get("name")
            if not isinstance(name, str) or unicodedata.normalize("NFC", name) not in source_text:
                continue
            evidence = entity.get("evidence")
            if not isinstance(evidence, list):
                continue
            grounded_evidence = [
                item for item in evidence if _exact_section_evidence(section_text, item)
            ]
            if grounded_evidence:
                grounded_entities.append({**entity, "evidence": grounded_evidence})
        cleaned["entities"] = grounded_entities

    return cleaned


def _relation_evidence_matches_link(evidence: dict[str, Any], link: Link) -> bool:
    quote = evidence.get("quote")
    if evidence.get("section_id") != link.from_section_id or not isinstance(quote, str):
        return False
    if quote not in link.evidence_quote:
        return False
    for match in MARKDOWN_LINK_RE.finditer(quote):
        target = match.group(1).split("#", maxsplit=1)[0]
        if PurePosixPath(target).stem == link.to_doc_id:
            return True
    return False


def validate_grounding(
    config: WikiConfig,
    document: Document,
    links: list[Link],
    record: dict[str, Any],
) -> None:
    core_keys = (
        "doc_id",
        "summary",
        "key_points",
        "keywords",
        "entities",
        "faq",
        "relations",
    )
    core = {key: record[key] for key in core_keys if key in record}
    errors: list[str] = []
    if core.get("doc_id") != document.doc_id:
        errors.append(f"doc_id가 요청과 다릅니다: {core.get('doc_id')}")
    schema = model_response_schema(config)
    for error in sorted(
        Draft202012Validator(schema).iter_errors(core),
        key=lambda item: list(item.absolute_path),
    ):
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        errors.append(f"schema {location}: {error.message}")
    if errors:
        raise ValidationError("enrichment 구조 검증 실패\n- " + "\n- ".join(errors[:40]))
    record = core
    raw_output = json.dumps(record, ensure_ascii=False)
    for forbidden in FORBIDDEN_OUTPUT:
        if forbidden.casefold() in raw_output.casefold():
            errors.append(f"금지된 출력이 포함되었습니다: {forbidden}")
    section_text = {section.section_id: section.evidence_text for section in document.sections}
    for evidence in _iter_evidence(record):
        section_id = evidence.get("section_id")
        quote = evidence.get("quote")
        if section_id not in section_text:
            errors.append(f"존재하지 않는 section_id: {section_id}")
        elif not isinstance(quote, str) or quote not in section_text[section_id]:
            errors.append(f"원문에 정확히 존재하지 않는 인용문: {quote!r}")
    source_text = _document_source_text(document)
    for keyword in record.get("keywords", []):
        if unicodedata.normalize("NFC", str(keyword)) not in source_text:
            errors.append(f"원문에 없는 키워드: {keyword}")
    for entity in record.get("entities", []):
        name = unicodedata.normalize("NFC", str(entity.get("name", "")))
        if name not in source_text:
            errors.append(f"원문에 없는 엔티티: {name}")
    for text in _generated_texts(record):
        for number in NUMBER_RE.findall(text):
            if number not in source_text:
                errors.append(f"원문에 없는 숫자 또는 날짜 구성요소: {number}")
    summary = record.get("summary", {}).get("text", "")
    sentence_count = len(SENTENCE_END_RE.findall(summary.strip()))
    limits = config.enrichment
    if (
        not int(limits["summary_min_sentences"])
        <= sentence_count
        <= int(limits["summary_max_sentences"])
    ):
        errors.append(f"요약 문장 수가 2~3개가 아닙니다: {sentence_count}")
    candidate_targets = {link.to_doc_id for link in links}
    relation_targets = [item.get("to_doc_id") for item in record.get("relations", [])]
    if set(relation_targets) != candidate_targets or len(relation_targets) != len(
        candidate_targets
    ):
        errors.append(
            "relations는 링크 후보 각각을 정확히 한 번 포함해야 합니다: "
            f"후보={sorted(candidate_targets)}, 출력={relation_targets}"
        )
    for relation in record.get("relations", []):
        target = relation.get("to_doc_id")
        matching_links = [link for link in links if link.to_doc_id == target]
        if not matching_links:
            continue
        grounded = any(
            _relation_evidence_matches_link(evidence, link)
            for evidence in relation.get("evidence", [])
            for link in matching_links
        )
        if not grounded:
            errors.append(f"관계 {target}의 근거가 명시적 링크 문장과 일치하지 않습니다")
    if errors:
        raise ValidationError("enrichment 근거 검증 실패\n- " + "\n- ".join(errors[:40]))


def _request_payload(document: Document, links: list[Link]) -> dict[str, Any]:
    metadata = {key: value for key, value in document.metadata.items() if key not in {"source"}}
    return {
        "document": metadata,
        "sections": [
            {
                "section_id": section.section_id,
                "source_chunk_no": section.source_chunk_no,
                "heading_path": list(section.heading_path),
                "text": section.evidence_text,
            }
            for section in document.sections
        ],
        "link_candidates": [
            {
                "to_doc_id": link.to_doc_id,
                "section_id": link.from_section_id,
                "evidence_quote": link.evidence_quote,
            }
            for link in links
        ],
    }


def _check_provider_ready(config: WikiConfig) -> str:
    get_api_key(config, required=True)
    fingerprint = model_fingerprint(config)
    return fingerprint


def _enforce_external_processing_policy(
    config: WikiConfig,
    documents: list[Document],
) -> None:
    if bool(config.scope.get("synthetic_corpus", False)):
        return
    access_levels = {str(document.metadata["access"]) for document in documents}
    if config.security["block_restricted"] and "restricted" in access_levels:
        raise ValidationError("restricted 문서는 외부 LLM으로 전송할 수 없습니다")
    allowed = set(config.security["external_llm_allowed_access"])
    disallowed = sorted(access_levels - allowed)
    if disallowed:
        raise ValidationError(
            "외부 LLM 전송이 허용되지 않은 access 등급이 있습니다: " + ", ".join(disallowed)
        )
    if (
        config.security["require_paid_policy_for_nonpublic"]
        and access_levels - {"public"}
        and config.provider["data_policy"] != config.security["paid_policy_name"]
    ):
        raise ValidationError(
            "비공개 문서는 provider.data_policy를 "
            f"{config.security['paid_policy_name']!r}로 확인한 배포에서만 처리할 수 있습니다"
        )


def _aggregate_usage(usages: list[dict[str, Any]]) -> dict[str, Any]:
    if not usages:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost": 0.0,
            "currency": "USD",
        }
    return {
        "input_tokens": sum(int(item.get("input_tokens", 0)) for item in usages),
        "output_tokens": sum(int(item.get("output_tokens", 0)) for item in usages),
        "total_tokens": sum(int(item.get("total_tokens", 0)) for item in usages),
        "estimated_cost": round(sum(float(item.get("estimated_cost", 0.0)) for item in usages), 8),
        "currency": str(usages[0].get("currency", "USD")),
        "pricing_effective_on": str(usages[0].get("pricing_effective_on", "")),
    }


def enrich_documents(
    config: WikiConfig,
    selected_ids: set[str] | None = None,
    refresh: bool = False,
    progress: Callable[[str], None] | None = None,
    scope: WikiScope | None = None,
) -> EnrichmentRun:
    if scope is not None:
        config = scoped_config(config, scope)
    documents = load_documents(config)
    known_ids = {document.doc_id for document in documents}
    if selected_ids:
        unknown = sorted(selected_ids - known_ids)
        if unknown:
            raise ValidationError(f"존재하지 않는 문서 ID: {', '.join(unknown)}")
    selected_documents = [
        document
        for document in documents
        if selected_ids is None or document.doc_id in selected_ids
    ]
    _enforce_external_processing_policy(config, selected_documents)
    fingerprint = _check_provider_ready(config)
    all_links = extract_links(documents)
    links_by_doc = {
        document.doc_id: [link for link in all_links if link.from_doc_id == document.doc_id]
        for document in documents
    }
    approved_dir = config.resolve_path("enrichment_dir") / "approved"
    review_dir = config.resolve_path("enrichment_dir") / "needs-review"
    approved_dir.mkdir(parents=True, exist_ok=True)
    review_dir.mkdir(parents=True, exist_ok=True)
    run = EnrichmentRun()
    pending: list[Document] = []
    for document in documents:
        if selected_ids is not None and document.doc_id not in selected_ids:
            continue
        approved_path = approved_dir / f"{document.doc_id}.json"
        if approved_path.is_file() and not refresh:
            try:
                existing = json.loads(approved_path.read_text(encoding="utf-8"))
                validate_record(config, "enrichment", existing, document.doc_id)
                validate_grounding(config, document, links_by_doc[document.doc_id], existing)
                if all(
                    (
                        existing["source_sha256"] == document.original_sha256,
                        compatible_model_fingerprint(config, existing["model_fingerprint"]),
                        existing["prompt_version"] == config.prompt_version,
                    )
                ):
                    run.skipped.append(document.doc_id)
                    continue
            except (json.JSONDecodeError, ValidationError):
                pass
        pending.append(document)
    if not pending:
        return run
    system_prompt = config.resolve_path("prompt_file").read_text(encoding="utf-8")
    response_schema = model_response_schema(config)
    max_attempts = int(config.enrichment["max_attempts"])
    with GeminiClient(config) as client:
        for document_number, document in enumerate(pending, start=1):
            if progress:
                progress(f"[{document_number}/{len(pending)}] {document.doc_id} 생성 시작")
            request = _request_payload(document, links_by_doc[document.doc_id])
            previous_output: str | None = None
            attempts: list[dict[str, Any]] = []
            attempt_usages: list[dict[str, Any]] = []
            approved_record: dict[str, Any] | None = None
            for attempt_number in range(1, max_attempts + 1):
                user_value: dict[str, Any] = request
                if previous_output is not None:
                    user_value = {
                        **request,
                        "correction": {
                            "instruction": (
                                "아래 검증 오류를 모두 수정해 JSON 전체를 다시 출력하세요."
                            ),
                            "previous_invalid_output": previous_output,
                            "validation_errors": attempts[-1]["error"],
                        },
                    }
                try:
                    completion = client.complete(
                        system_prompt,
                        json.dumps(user_value, ensure_ascii=False, separators=(",", ":")),
                        response_schema,
                    )
                    raw = completion.text
                    attempt_usages.append(completion.usage)
                    previous_output = raw
                    core = sanitize_optional_grounding(document, json.loads(raw))
                    validate_grounding(config, document, links_by_doc[document.doc_id], core)
                    approved_record = {
                        "schema_version": config.schema_version,
                        "doc_id": document.doc_id,
                        "source_sha256": document.original_sha256,
                        "model_fingerprint": fingerprint,
                        "prompt_version": config.prompt_version,
                        "generation": {
                            "provider": config.provider["name"],
                            "model": config.provider["model"],
                            "attempt_count": attempt_number,
                            "usage": _aggregate_usage(attempt_usages),
                        },
                        "summary": core["summary"],
                        "key_points": core["key_points"],
                        "keywords": core["keywords"],
                        "entities": core["entities"],
                        "faq": core["faq"],
                        "relations": core["relations"],
                    }
                    validate_record(config, "enrichment", approved_record, document.doc_id)
                    break
                except ProviderError as exc:
                    attempts.append(
                        {
                            "attempt": attempt_number,
                            "error": str(exc),
                            "raw_output": previous_output,
                        }
                    )
                    raise
                except (json.JSONDecodeError, ValidationError) as exc:
                    attempts.append(
                        {
                            "attempt": attempt_number,
                            "error": str(exc),
                            "raw_output": previous_output,
                        }
                    )
            approved_path = approved_dir / f"{document.doc_id}.json"
            review_path = review_dir / f"{document.doc_id}.json"
            if approved_record is not None:
                atomic_write_json(approved_path, approved_record)
                if review_path.is_file():
                    review_path.unlink()
                run.processed.append(document.doc_id)
                if progress:
                    progress(f"[{document_number}/{len(pending)}] {document.doc_id} 승인")
            else:
                # Keep an older approved record as a recovery cache. Its source SHA,
                # prompt and model fingerprint are checked before every use, so it is
                # excluded from the failed candidate but can be reused after Undo.
                atomic_write_json(
                    review_path,
                    {
                        "schema_version": config.schema_version,
                        "doc_id": document.doc_id,
                        "source_sha256": document.original_sha256,
                        "model_fingerprint": fingerprint,
                        "prompt_version": config.prompt_version,
                        "generation": {
                            "provider": config.provider["name"],
                            "model": config.provider["model"],
                            "attempt_count": max(len(attempts), 1),
                            "usage": _aggregate_usage(attempt_usages),
                        },
                        "attempts": attempts,
                    },
                )
                run.failed.append(document.doc_id)
                if progress:
                    progress(
                        f"[{document_number}/{len(pending)}] {document.doc_id} 검토 대기로 이동"
                    )
    return run
