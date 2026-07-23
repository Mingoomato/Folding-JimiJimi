from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from wiki_builder.builder import prepare_build_data, render_to_temporary
from wiki_builder.config import WikiConfig, WikiScope, default_scope, scoped_config
from wiki_builder.corpus import discover_links
from wiki_builder.enrichment import validate_grounding
from wiki_builder.errors import ValidationError
from wiki_builder.io_utils import GENERATOR_MARKER
from wiki_builder.markdown import load_documents
from wiki_builder.schemas import validate_record
from wiki_builder.state import load_current_build


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValidationError(f"필수 JSONL 파일이 없습니다: {path}")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"{path}:{line_number}: 잘못된 JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise ValidationError(f"{path}:{line_number}: JSON 객체가 아닙니다")
        records.append(value)
    return records


def _relative_files(root: Path) -> list[Path]:
    return sorted(
        (path.relative_to(root) for path in root.rglob("*") if path.is_file()),
        key=lambda item: item.as_posix(),
    )


def _validate_reproducible(
    config: WikiConfig,
    output_dir: Path,
    scope: WikiScope,
) -> None:
    with tempfile.TemporaryDirectory(prefix="wiki-verify-", dir=config.project_dir) as temp:
        _, temporary_root = render_to_temporary(config, Path(temp), scope)
        expected_files = _relative_files(temporary_root)
        actual_files = _relative_files(output_dir)
        if expected_files != actual_files:
            missing = sorted(set(expected_files) - set(actual_files))
            extra = sorted(set(actual_files) - set(expected_files))
            raise ValidationError(
                "재현 빌드 파일 목록 불일치: "
                f"누락={[item.as_posix() for item in missing]}, "
                f"추가={[item.as_posix() for item in extra]}"
            )
        changed = [
            path.as_posix()
            for path in expected_files
            if (temporary_root / path).read_bytes() != (output_dir / path).read_bytes()
        ]
        if changed:
            raise ValidationError("재현 빌드 바이트 불일치: " + ", ".join(changed))


def validate_wiki(
    config: WikiConfig,
    require_enrichment: bool = True,
    check_reproducible: bool = True,
    scope: WikiScope | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    selected_scope = scope or default_scope(config)
    active_config = scoped_config(config, selected_scope)
    if output_dir is None:
        current_build = load_current_build(config, selected_scope)
        if current_build is None:
            raise ValidationError(
                f"현재 활성화된 빌드가 없습니다: {selected_scope.current_pointer}"
            )
        _, output_dir = current_build
    if not output_dir.is_dir() or not (output_dir / GENERATOR_MARKER).is_file():
        raise ValidationError(f"빌더가 생성한 위키 폴더가 아닙니다: {output_dir}")
    expected = prepare_build_data(active_config)
    manifest = _read_jsonl(output_dir / "manifest.jsonl")
    chunks = _read_jsonl(output_dir / "retrieval" / "chunks.jsonl")
    links = _read_jsonl(output_dir / "retrieval" / "links.jsonl")
    enrichments = _read_jsonl(output_dir / "retrieval" / "enrichments.jsonl")
    aliases = json.loads((output_dir / "retrieval" / "aliases.json").read_text(encoding="utf-8"))
    for record in manifest:
        validate_record(active_config, "manifest", record, record.get("doc_id", "manifest"))
    for record in chunks:
        validate_record(active_config, "chunk", record, record.get("chunk_id", "chunk"))
    for record in links:
        validate_record(active_config, "link", record, "link")
    validate_record(active_config, "aliases", aliases, "aliases.json")
    for record in enrichments:
        validate_record(active_config, "enrichment", record, record.get("doc_id", "enrichment"))
    comparisons = {
        "manifest.jsonl": (manifest, expected.manifest),
        "chunks.jsonl": (chunks, expected.chunks),
        "links.jsonl": (links, expected.links),
        "aliases.json": (aliases, expected.aliases),
        "enrichments.jsonl": (enrichments, expected.enrichments),
    }
    changed = [name for name, (actual, wanted) in comparisons.items() if actual != wanted]
    if changed:
        raise ValidationError("현재 입력으로 다시 계산한 데이터와 불일치: " + ", ".join(changed))
    documents = load_documents(active_config)
    document_by_id = {document.doc_id: document for document in documents}
    explicit_links = discover_links(active_config, documents)
    for record in enrichments:
        doc_id = record["doc_id"]
        validate_grounding(
            active_config,
            document_by_id[doc_id],
            [link for link in explicit_links if link.from_doc_id == doc_id],
            record,
        )
    for document in documents:
        output_path = output_dir / document.output_relative_path
        if not output_path.is_file():
            raise ValidationError(f"정규화 문서 누락: {document.output_relative_path}")
        if output_path.read_text(encoding="utf-8") != document.normalized_markdown:
            raise ValidationError(f"정규화 문서 내용 불일치: {document.output_relative_path}")
    statuses = {record["doc_id"]: record["enrichment"]["status"] for record in manifest}
    nonapproved = sorted(doc_id for doc_id, status in statuses.items() if status != "approved")
    review_dir = active_config.resolve_path("enrichment_dir") / "needs-review"
    review_files = sorted(path.name for path in review_dir.glob("*.json"))
    if require_enrichment and (nonapproved or review_files):
        raise ValidationError(
            f"enrichment 전체 승인 조건 미충족: 미승인={nonapproved}, 검토대기={review_files}"
        )
    build_meta = json.loads((output_dir / "build-meta.json").read_text(encoding="utf-8"))
    validate_record(active_config, "build-meta", build_meta, build_meta.get("build_id", "build"))
    if build_meta["tenant_id"] != selected_scope.tenant_id:
        raise ValidationError("build-meta tenant_id가 요청 범위와 다릅니다")
    if build_meta["wiki_id"] != selected_scope.wiki_id:
        raise ValidationError("build-meta wiki_id가 요청 범위와 다릅니다")
    if build_meta["build_id"] != output_dir.name:
        raise ValidationError("build-meta build_id가 빌드 디렉터리 이름과 다릅니다")
    if check_reproducible:
        _validate_reproducible(config, output_dir, selected_scope)
    return {
        "tenant_id": selected_scope.tenant_id,
        "wiki_id": selected_scope.wiki_id,
        "build_id": build_meta["build_id"],
        "documents": len(manifest),
        "source_fragments": sum(record["source_fragment_count"] for record in manifest),
        "chunks": len(chunks),
        "aliases": len(aliases["entries"]),
        "links": len(links),
        "enrichments": len(enrichments),
        "nonapproved": nonapproved,
        "needs_review": review_files,
        "reproducible": check_reproducible,
        "complete": bool(build_meta["complete"]),
    }
