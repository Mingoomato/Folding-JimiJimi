from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from codegate_api.files.atomic import DocumentLockManager, SafeSourceFileStore
from codegate_api.files.resolver import SourceUriResolver
from codegate_api.integrations.doc2md import ConversionResult
from codegate_api.knowledge.access import AccessContext
from codegate_api.knowledge.llmwiki import (
    LLMWikiCompatibilityAdapter,
    LLMWikiCompatibilityError,
    NativeLLMWikiCatalog,
    NativeLLMWikiPipeline,
)
from codegate_api.knowledge.repository import KnowledgePackageError, KnowledgeRepository
from codegate_api.state.store import PendingEvent, StateStore


@dataclass(frozen=True)
class NativeFixture:
    adapter: LLMWikiCompatibilityAdapter
    source_root: Path
    source_path: Path
    input_path: Path
    compatibility_root: Path
    before_sha256: str


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n" for value in values),
        encoding="utf-8",
    )


def _frontmatter(
    source_sha256: str,
    body: str,
    *,
    revision: str = "1",
    chunk_no: str | None = None,
) -> str:
    metadata = {
        "schema_version": "1.0.0",
        "id": "REG-100001",
        "title": "개인정보 처리 규정",
        "doc_type": "regulation",
        "language": "ko",
        "revision": revision,
        "status": "active",
        "official_number": None,
        "authority_level": "regulation",
        "issuing_org": "정보보호위원회",
        "issued_on": None,
        "effective_from": None,
        "effective_to": None,
        "source": {
            "filename": "regulations/REG-100001.md",
            "uri": "source://regulations/REG-100001.md",
            "sha256": source_sha256,
        },
        "access": "public",
        "tags": ["개인정보"],
        "aliases": ["보관 규정"],
    }
    if chunk_no is not None:
        metadata["chunk_no"] = chunk_no
    return "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) + "---\n" + body


def _create_native_fixture(tmp_path: Path) -> NativeFixture:
    source_root = tmp_path / "source"
    source_path = source_root / "regulations/REG-100001.md"
    source_path.parent.mkdir(parents=True)
    source_content = "# local source\n\n개인정보는 1년간 보관한다.\n".encode()
    source_path.write_bytes(source_content)
    source_sha256 = _sha256(source_content)

    input_root = tmp_path / "input"
    input_root.mkdir()
    input_path = input_root / "REG-100001.md"
    body = (
        '# 개인정보 처리 규정\n\n<a id="art-1"></a>\n## 보관 기간\n\n개인정보는 1년간 보관한다.\n'
    )
    input_text = _frontmatter(source_sha256, body)
    input_path.write_text(input_text, encoding="utf-8", newline="\n")

    build_id = "build-0123456789abcdefabcd"
    storage_root = tmp_path / "storage"
    wiki_root = storage_root / "tenants/local/wikis/workspace"
    build_dir = wiki_root / "builds" / build_id
    canonical_path = build_dir / "docs/regulations/REG-100001.md"
    canonical_text = _frontmatter(
        source_sha256,
        "# 개인정보 처리 규정\n\n"
        '<a id="art-1"></a>\n'
        '<a id="sec-0123456789"></a>\n'
        "## 보관 기간\n\n개인정보는 1년간 보관한다.\n",
    )
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.write_text(canonical_text, encoding="utf-8", newline="\n")
    (build_dir / "README.md").write_text("# Native LLMWIKI\n", encoding="utf-8", newline="\n")

    manifest = {
        "schema_version": "1.0.0",
        "wiki_version": "1.0.0",
        "doc_id": "REG-100001",
        "revision": "1",
        "title": "개인정보 처리 규정",
        "doc_type": "regulation",
        "language": "ko",
        "status": "active",
        "official_number": None,
        "authority_level": "regulation",
        "issuing_org": "정보보호위원회",
        "issued_on": None,
        "effective_from": None,
        "effective_to": None,
        "path": "docs/regulations/REG-100001.md",
        "access": "public",
        "tags": ["개인정보"],
        "aliases": ["보관 규정"],
        "source": {
            "filename": "regulations/REG-100001.md",
            "uri": "source://regulations/REG-100001.md",
            "sha256": source_sha256,
            "verification": "verified",
        },
        "ingest": {
            "path": "source-md/REG-100001.md",
            "sha256": _sha256(input_text.encode()),
        },
        "content_sha256": _sha256(canonical_text.encode()),
        "section_count": 1,
        "chunk_count": 1,
        "link_count": 0,
        "summary": None,
        "keywords": [],
        "enrichment": {
            "status": "pending",
            "model": None,
            "prompt_version": None,
            "record_ref": None,
        },
    }
    chunk_text = "개인정보는 1년간 보관한다."
    chunk = {
        "schema_version": "1.0.0",
        "wiki_version": "1.0.0",
        "chunk_id": "REG-100001@1#sec-0123456789:c001",
        "doc_id": "REG-100001",
        "revision": "1",
        "ordinal": 1,
        "section_id": "sec-0123456789",
        "heading_path": ["개인정보 처리 규정", "보관 기간"],
        "chunk_type": "section",
        "text": chunk_text,
        "embedding_text": f"개인정보 처리 규정\n보관 기간\n{chunk_text}",
        "char_count": len(chunk_text),
        "source_path": "docs/regulations/REG-100001.md",
        "content_sha256": _sha256(chunk_text.encode()),
        "status": "active",
        "effective_from": None,
        "effective_to": None,
        "tags": ["개인정보"],
        "access": "public",
    }
    _write_jsonl(build_dir / "manifest.jsonl", [manifest])
    _write_jsonl(build_dir / "retrieval/chunks.jsonl", [chunk])
    _write_jsonl(build_dir / "retrieval/links.jsonl", [])
    _write_json(
        build_dir / "retrieval/aliases.json",
        {
            "schema_version": "1.0.0",
            "normalization": "NFC+whitespace",
            "entries": [
                {
                    "term": "보관 규정",
                    "targets": [{"doc_id": "REG-100001", "kind": "declared"}],
                }
            ],
        },
    )
    _write_json(
        build_dir / "build-meta.json",
        {
            "schema_version": "1.0.0",
            "wiki_version": "1.0.0",
            "build_format_version": "server-v1",
            "tenant_id": "local",
            "wiki_id": "workspace",
            "build_id": build_id,
            "complete": False,
        },
    )
    permissive_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
    }
    for name in ("current", "build-meta", "manifest", "chunk", "link", "aliases"):
        _write_json(build_dir / f"schemas/{name}.schema.json", permissive_schema)
    _write_json(
        wiki_root / "current.json",
        {
            "schema_version": "1.0.0",
            "tenant_id": "local",
            "wiki_id": "workspace",
            "build_id": build_id,
            "relative_path": f"builds/{build_id}",
            "complete": False,
        },
    )

    compatibility_root = tmp_path / "compatibility"
    adapter = LLMWikiCompatibilityAdapter(
        source_resolver=SourceUriResolver(source_root),
        input_root=input_root,
        storage_root=storage_root,
        compatibility_root=compatibility_root,
        tenant_id="local",
        wiki_id="workspace",
    )
    return NativeFixture(
        adapter=adapter,
        source_root=source_root,
        source_path=source_path,
        input_path=input_path,
        compatibility_root=compatibility_root,
        before_sha256=source_sha256,
    )


def _use_server_v2_ingest(
    fixture: NativeFixture,
    fragments: list[tuple[Path, str | None]] | None = None,
) -> None:
    current = fixture.adapter.current_native_build()
    assert current is not None
    selected = fragments or [(fixture.input_path, None)]
    records = [
        {
            "chunk_no": chunk_no,
            "path": f"source-md/{path.name}",
            "sha256": _sha256(path.read_bytes()),
        }
        for path, chunk_no in selected
    ]
    mode = (
        "sections" if len(records) == 1 and records[0]["chunk_no"] is None else "source-fragments"
    )
    if mode == "sections":
        aggregate_sha256 = str(records[0]["sha256"])
    else:
        aggregate_sha256 = _sha256(
            json.dumps(
                [
                    {"chunk_no": record["chunk_no"], "sha256": record["sha256"]}
                    for record in records
                ],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
    manifest_path = current.build_dir / "manifest.jsonl"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["ingest"] = {
        "aggregate_sha256": aggregate_sha256,
        "fragments": records,
    }
    manifest["source_fragment_count"] = len(records)
    manifest["chunking_mode"] = mode
    _write_jsonl(manifest_path, [manifest])
    metadata_path = current.build_dir / "build-meta.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["build_format_version"] = "server-v2"
    _write_json(metadata_path, metadata)


def _build_pipeline(fixture: NativeFixture, tmp_path: Path) -> NativeLLMWikiPipeline:
    state = StateStore(tmp_path / "state.sqlite3")
    state.initialize()
    catalog = NativeLLMWikiCatalog(
        adapter=fixture.adapter,
        runner=object(),  # type: ignore[arg-type]
        source_resolver=SourceUriResolver(fixture.source_root),
        state_store=state,
    )
    catalog.bootstrap()
    return NativeLLMWikiPipeline(
        catalog=catalog,
        file_store=SafeSourceFileStore(
            SourceUriResolver(fixture.source_root),
            backup_root=tmp_path / "backups",
            max_bytes=1_000_000,
        ),
        state_store=state,
        lock_manager=DocumentLockManager(tmp_path / "locks"),
    )


def _clone_native_build(
    fixture: NativeFixture,
    build_id: str,
    *,
    ingest_sha256: str | None = None,
) -> Path:
    active = fixture.adapter.current_native_build()
    assert active is not None
    candidate_dir = active.build_dir.parent / build_id
    shutil.copytree(active.build_dir, candidate_dir)
    build_meta = json.loads((candidate_dir / "build-meta.json").read_text(encoding="utf-8"))
    build_meta["build_id"] = build_id
    _write_json(candidate_dir / "build-meta.json", build_meta)
    if ingest_sha256 is not None:
        manifest_path = candidate_dir / "manifest.jsonl"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["ingest"]["sha256"] = ingest_sha256
        _write_jsonl(manifest_path, [manifest])
    return candidate_dir


class _ActivatingRunner:
    def __init__(
        self,
        fixture: NativeFixture,
        *,
        fail_after_commit: bool = False,
        tamper_after_commit: bool = False,
    ) -> None:
        self._fixture = fixture
        self._fail_after_commit = fail_after_commit
        self._tamper_after_commit = tamper_after_commit
        self.activations: list[tuple[str, str | None]] = []

    def activate(self, *, build_id: str, expected_current_build_id: str | None) -> None:
        current = self._fixture.adapter.current_native_build()
        actual = current.build_id if current is not None else None
        if actual != expected_current_build_id:
            raise LLMWikiCompatibilityError("stale activation")
        build = self._fixture.adapter.native_build(build_id)
        self.activations.append((build_id, expected_current_build_id))
        _write_json(
            self._fixture.adapter.wiki_root / "current.json",
            {
                "schema_version": "1.0.0",
                "tenant_id": "local",
                "wiki_id": "workspace",
                "build_id": build_id,
                "relative_path": f"builds/{build_id}",
                "complete": build.complete,
            },
        )
        if self._tamper_after_commit:
            (build.build_dir / "README.md").write_text("tampered after commit", encoding="utf-8")
        if self._fail_after_commit:
            raise LLMWikiCompatibilityError("audit write failed after pointer commit")


class _PipelineState:
    def __init__(self, event: PendingEvent, *, fail_completion_once: bool = False) -> None:
        self._event = event
        self._fail_completion_once = fail_completion_once
        self._completion_failed = False
        self.completed: tuple[list[str], str] | None = None

    def pending_events(self) -> list[PendingEvent]:
        return [self._event]

    def begin_event_batch(self, _event_ids: list[str]) -> None:
        return None

    def record_sync_task(self, *_args: object, **kwargs: object) -> None:
        if (
            self._fail_completion_once
            and not self._completion_failed
            and kwargs.get("stage") == "publish"
            and kwargs.get("status") == "succeeded"
        ):
            self._completion_failed = True
            raise OSError("state store temporarily unavailable")
        return None

    def record_knowledge_version(self, *_args: object, **_kwargs: object) -> None:
        return None

    def complete_event_batch(self, event_ids: list[str], version: str) -> None:
        self.completed = (event_ids, version)

    def fail_event_batch(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("successful pipeline must not fail its batch")


def _event(
    fixture: NativeFixture,
    *,
    event_id: str,
    before_sha256: str,
    after_sha256: str,
    expected_text: str,
    replacement_text: str,
) -> PendingEvent:
    execution_id = f"exec_{event_id}"
    return PendingEvent(
        event_id=event_id,
        execution_id=execution_id,
        attempts=0,
        payload={
            "schema_version": "1.0.0",
            "event_id": event_id,
            "execution_id": execution_id,
            "document_id": "REG-100001",
            "source_uri": "source://regulations/REG-100001.md",
            "before_sha256": before_sha256,
            "after_sha256": after_sha256,
            "file_version_id": f"fv_{after_sha256}",
            "operation": {
                "type": "replace_exact",
                "expected_text": expected_text,
                "replacement_text": replacement_text,
                "expected_occurrences": 1,
            },
        },
    )


def test_native_llmwiki_build_is_mapped_to_valid_read_cache(tmp_path: Path) -> None:
    fixture = _create_native_fixture(tmp_path)
    build = fixture.adapter.current_native_build()

    assert build is not None
    package_root = fixture.adapter.materialize(build)
    repository = KnowledgeRepository(
        package_root,
        SourceUriResolver(fixture.source_root),
    )
    repository.load()

    assert repository.version == build.build_id
    assert repository.index_artifacts_available is True
    result = repository.get("REG-100001", access_context=AccessContext.anonymous())
    assert result is not None
    assert result.evidence[0].quote == "개인정보는 1년간 보관한다."
    source = repository.read_source_text(
        "REG-100001",
        access_context=AccessContext.anonymous(),
    )
    assert source is not None
    assert "1년간" in source[1]
    assert fixture.adapter.input_path(build, "REG-100001") == fixture.input_path
    canonical = (package_root / "docs/regulations/REG-100001.md").read_text(encoding="utf-8")
    assert '<span id="art-1"></span>' in canonical
    assert canonical.count('<a id="sec-0123456789"></a>') == 1


def test_native_adapter_accepts_current_server_v2_build_format(tmp_path: Path) -> None:
    fixture = _create_native_fixture(tmp_path)
    _use_server_v2_ingest(fixture)
    current = fixture.adapter.current_native_build()
    assert current is not None

    build = fixture.adapter.native_build("build-0123456789abcdefabcd")
    baseline = fixture.adapter.input_baseline(build, "REG-100001")

    assert build.build_id == "build-0123456789abcdefabcd"
    assert baseline.path == fixture.input_path
    assert baseline.file_sha256 == _sha256(fixture.input_path.read_bytes())


def test_native_adapter_marks_server_v2_source_fragments_read_only(tmp_path: Path) -> None:
    fixture = _create_native_fixture(tmp_path)
    source_sha256 = fixture.before_sha256
    first = _frontmatter(
        source_sha256,
        "# 개인정보 처리 규정\n\n첫 번째 의미 단위다.\n",
        chunk_no="chunk-001",
    )
    second_path = fixture.input_path.with_name("REG-100001_002.md")
    second = _frontmatter(
        source_sha256,
        "<!-- section: 후속 의미 단위 -->\n\n두 번째 의미 단위다.\n",
        chunk_no="chunk-002",
    )
    fixture.input_path.write_text(first, encoding="utf-8", newline="\n")
    second_path.write_text(second, encoding="utf-8", newline="\n")
    _use_server_v2_ingest(
        fixture,
        [(fixture.input_path, "chunk-001"), (second_path, "chunk-002")],
    )
    build = fixture.adapter.current_native_build()
    assert build is not None

    package_root = fixture.adapter.materialize(build)
    manifest = json.loads((package_root / "manifest.jsonl").read_text(encoding="utf-8"))

    assert manifest["write_access"] == "none"
    assert manifest["editability"] == "read_only"
    with pytest.raises(LLMWikiCompatibilityError, match="source-fragment document is read-only"):
        fixture.adapter.input_baseline(build, "REG-100001")

    pipeline = _build_pipeline(fixture, tmp_path)
    changed_source = fixture.source_path.read_text(encoding="utf-8").replace("1년", "3년")
    fixture.source_path.write_text(changed_source, encoding="utf-8", newline="\n")
    event = _event(
        fixture,
        event_id="evt_source_fragments",
        before_sha256=fixture.before_sha256,
        after_sha256=_sha256(changed_source.encode()),
        expected_text="1년",
        replacement_text="3년",
    )
    with pytest.raises(LLMWikiCompatibilityError, match="source-fragment document is read-only"):
        pipeline._apply_inputs([event])  # noqa: SLF001
    assert fixture.adapter.current_native_build().build_id == build.build_id  # type: ignore[union-attr]


def test_native_adapter_rejects_invalid_server_v2_fragment_aggregate(tmp_path: Path) -> None:
    fixture = _create_native_fixture(tmp_path)
    _use_server_v2_ingest(fixture)
    current = fixture.adapter.current_native_build()
    assert current is not None
    manifest_path = current.build_dir / "manifest.jsonl"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["ingest"]["aggregate_sha256"] = "f" * 64
    _write_jsonl(manifest_path, [manifest])

    with pytest.raises(LLMWikiCompatibilityError, match="section ingest metadata is invalid"):
        fixture.adapter.input_baseline(
            fixture.adapter.native_build(current.build_id),
            "REG-100001",
        )


def test_native_catalog_prepares_before_cas_activation(tmp_path: Path) -> None:
    fixture = _create_native_fixture(tmp_path)
    candidate_id = "build-11111111111111111111"
    _clone_native_build(fixture, candidate_id)
    runner = _ActivatingRunner(fixture)
    state = StateStore(tmp_path / "state.sqlite3")
    state.initialize()
    catalog = NativeLLMWikiCatalog(
        adapter=fixture.adapter,
        runner=runner,  # type: ignore[arg-type]
        source_resolver=SourceUriResolver(fixture.source_root),
        state_store=state,
    )
    catalog.bootstrap()
    parent_version = catalog.snapshot().version

    prepared = catalog.prepare_candidate(
        candidate_id,
        expected_current_build_id=parent_version,
        changed_document_ids=set(),
    )

    assert catalog.snapshot().version == parent_version
    assert fixture.adapter.current_native_build().build_id == parent_version  # type: ignore[union-attr]
    assert state.llmwiki_build_digest(candidate_id) == prepared.native_digest
    repository = catalog.activate_candidate(
        prepared,
        expected_current_build_id=parent_version,
    )
    assert repository.version == candidate_id
    assert catalog.snapshot().version == candidate_id
    assert runner.activations == [(candidate_id, parent_version)]


def test_native_catalog_rejects_unrelated_candidate_input(tmp_path: Path) -> None:
    fixture = _create_native_fixture(tmp_path)
    candidate_id = "build-22222222222222222222"
    _clone_native_build(fixture, candidate_id, ingest_sha256="f" * 64)
    state = StateStore(tmp_path / "state.sqlite3")
    state.initialize()
    catalog = NativeLLMWikiCatalog(
        adapter=fixture.adapter,
        runner=object(),  # type: ignore[arg-type]
        source_resolver=SourceUriResolver(fixture.source_root),
        state_store=state,
    )
    catalog.bootstrap()
    parent_version = catalog.snapshot().version

    with pytest.raises(LLMWikiCompatibilityError, match="unrelated input"):
        catalog.prepare_candidate(
            candidate_id,
            expected_current_build_id=parent_version,
            changed_document_ids=set(),
        )

    assert fixture.adapter.current_native_build().build_id == parent_version  # type: ignore[union-attr]
    assert catalog.snapshot().version == parent_version


def test_native_catalog_rejects_unrelated_candidate_input_path_change(tmp_path: Path) -> None:
    fixture = _create_native_fixture(tmp_path)
    candidate_id = "build-88888888888888888888"
    candidate_dir = _clone_native_build(fixture, candidate_id)
    manifest_path = candidate_dir / "manifest.jsonl"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["ingest"]["path"] = "source-md/renamed.md"
    _write_jsonl(manifest_path, [manifest])
    state = StateStore(tmp_path / "state.sqlite3")
    state.initialize()
    catalog = NativeLLMWikiCatalog(
        adapter=fixture.adapter,
        runner=object(),  # type: ignore[arg-type]
        source_resolver=SourceUriResolver(fixture.source_root),
        state_store=state,
    )
    catalog.bootstrap()
    parent_version = catalog.snapshot().version

    with pytest.raises(LLMWikiCompatibilityError, match="unrelated input"):
        catalog.prepare_candidate(
            candidate_id,
            expected_current_build_id=parent_version,
            changed_document_ids=set(),
        )


def test_native_catalog_rechecks_sources_before_activation(tmp_path: Path) -> None:
    fixture = _create_native_fixture(tmp_path)
    candidate_id = "build-33333333333333333333"
    _clone_native_build(fixture, candidate_id)
    runner = _ActivatingRunner(fixture)
    state = StateStore(tmp_path / "state.sqlite3")
    state.initialize()
    catalog = NativeLLMWikiCatalog(
        adapter=fixture.adapter,
        runner=runner,  # type: ignore[arg-type]
        source_resolver=SourceUriResolver(fixture.source_root),
        state_store=state,
    )
    catalog.bootstrap()
    parent_version = catalog.snapshot().version
    prepared = catalog.prepare_candidate(
        candidate_id,
        expected_current_build_id=parent_version,
        changed_document_ids=set(),
    )
    fixture.source_path.write_text("changed outside approval", encoding="utf-8")

    with pytest.raises(KnowledgePackageError, match="source checksum mismatch"):
        catalog.activate_candidate(
            prepared,
            expected_current_build_id=parent_version,
        )

    assert runner.activations == []
    assert fixture.adapter.current_native_build().build_id == parent_version  # type: ignore[union-attr]
    assert catalog.snapshot().version == parent_version


def test_native_catalog_recovers_activation_committed_before_error(tmp_path: Path) -> None:
    fixture = _create_native_fixture(tmp_path)
    candidate_id = "build-55555555555555555555"
    _clone_native_build(fixture, candidate_id)
    runner = _ActivatingRunner(fixture, fail_after_commit=True)
    state = StateStore(tmp_path / "state.sqlite3")
    state.initialize()
    catalog = NativeLLMWikiCatalog(
        adapter=fixture.adapter,
        runner=runner,  # type: ignore[arg-type]
        source_resolver=SourceUriResolver(fixture.source_root),
        state_store=state,
    )
    catalog.bootstrap()
    parent_version = catalog.snapshot().version
    prepared = catalog.prepare_candidate(
        candidate_id,
        expected_current_build_id=parent_version,
        changed_document_ids=set(),
    )

    repository = catalog.activate_candidate(
        prepared,
        expected_current_build_id=parent_version,
    )

    assert repository.version == candidate_id
    assert fixture.adapter.current_native_build().build_id == candidate_id  # type: ignore[union-attr]
    assert catalog.snapshot().version == candidate_id


def test_native_catalog_rejects_candidate_changed_after_validation(tmp_path: Path) -> None:
    fixture = _create_native_fixture(tmp_path)
    candidate_id = "build-66666666666666666666"
    candidate_dir = _clone_native_build(fixture, candidate_id)
    runner = _ActivatingRunner(fixture)
    state = StateStore(tmp_path / "state.sqlite3")
    state.initialize()
    catalog = NativeLLMWikiCatalog(
        adapter=fixture.adapter,
        runner=runner,  # type: ignore[arg-type]
        source_resolver=SourceUriResolver(fixture.source_root),
        state_store=state,
    )
    catalog.bootstrap()
    parent_version = catalog.snapshot().version
    prepared = catalog.prepare_candidate(
        candidate_id,
        expected_current_build_id=parent_version,
        changed_document_ids=set(),
    )
    (candidate_dir / "README.md").write_text("tampered", encoding="utf-8")

    with pytest.raises(LLMWikiCompatibilityError, match="changed after validation"):
        catalog.activate_candidate(
            prepared,
            expected_current_build_id=parent_version,
        )

    assert runner.activations == []
    assert fixture.adapter.current_native_build().build_id == parent_version  # type: ignore[union-attr]
    assert catalog.snapshot().version == parent_version


def test_native_catalog_does_not_recover_tampered_committed_candidate(tmp_path: Path) -> None:
    fixture = _create_native_fixture(tmp_path)
    candidate_id = "build-77777777777777777777"
    _clone_native_build(fixture, candidate_id)
    runner = _ActivatingRunner(fixture, tamper_after_commit=True)
    state = StateStore(tmp_path / "state.sqlite3")
    state.initialize()
    catalog = NativeLLMWikiCatalog(
        adapter=fixture.adapter,
        runner=runner,  # type: ignore[arg-type]
        source_resolver=SourceUriResolver(fixture.source_root),
        state_store=state,
    )
    catalog.bootstrap()
    parent_version = catalog.snapshot().version
    prepared = catalog.prepare_candidate(
        candidate_id,
        expected_current_build_id=parent_version,
        changed_document_ids=set(),
    )

    with pytest.raises(LLMWikiCompatibilityError, match="differs from validation"):
        catalog.activate_candidate(
            prepared,
            expected_current_build_id=parent_version,
        )
    with pytest.raises(LLMWikiCompatibilityError, match="validated candidate"):
        catalog.recover_committed_candidate(prepared)

    assert fixture.adapter.current_native_build().build_id == candidate_id  # type: ignore[union-attr]
    assert catalog.snapshot().version == parent_version

    restarted = NativeLLMWikiCatalog(
        adapter=fixture.adapter,
        runner=runner,  # type: ignore[arg-type]
        source_resolver=SourceUriResolver(fixture.source_root),
        state_store=state,
    )
    with pytest.raises(LLMWikiCompatibilityError, match="persisted validation"):
        restarted.bootstrap()


@pytest.mark.parametrize("fail_completion_once", [False, True])
def test_native_pipeline_stages_validates_then_activates(
    tmp_path: Path,
    fail_completion_once: bool,
) -> None:
    fixture = _create_native_fixture(tmp_path)
    event = _event(
        fixture,
        event_id="evt_staged_publish",
        before_sha256=fixture.before_sha256,
        after_sha256="a" * 64,
        expected_text="1년",
        replacement_text="3년",
    )
    calls: list[str] = []

    class Repository:
        def __init__(self, version: str) -> None:
            self.version = version

    class TrackingLocks:
        def __init__(self) -> None:
            self.publish_held = False

        @asynccontextmanager
        async def acquire_publish(self):
            assert self.publish_held is False
            self.publish_held = True
            try:
                yield
            finally:
                self.publish_held = False

    locks = TrackingLocks()

    class Catalog:
        def __init__(self) -> None:
            self.repository = Repository("build-0123456789abcdefabcd")
            self.prepared = object()

        def snapshot(self) -> Repository:
            return self.repository

        def stage(self) -> str:
            assert locks.publish_held is False
            calls.append("stage")
            return "build-44444444444444444444"

        def prepare_candidate(self, *_args: object, **_kwargs: object) -> object:
            assert locks.publish_held is True
            calls.append("prepare")
            return self.prepared

        def activate_candidate(self, prepared: object, **_kwargs: object) -> Repository:
            assert locks.publish_held is True
            assert prepared is self.prepared
            calls.append("activate")
            self.repository = Repository("build-44444444444444444444")
            return self.repository

        def recover_committed_candidate(self, prepared: object) -> bool:
            assert locks.publish_held is True
            assert prepared is self.prepared
            calls.append("recover")
            return self.repository.version == "build-44444444444444444444"

        def refresh(self, **_kwargs: object) -> Repository:
            raise AssertionError("committed publish must recover without refresh")

    state = _PipelineState(event, fail_completion_once=fail_completion_once)
    catalog = Catalog()
    pipeline = NativeLLMWikiPipeline(
        catalog=catalog,  # type: ignore[arg-type]
        file_store=object(),  # type: ignore[arg-type]
        state_store=state,  # type: ignore[arg-type]
        lock_manager=locks,  # type: ignore[arg-type]
    )
    pipeline._apply_inputs = lambda _events: (
        calls.append("apply")
        or {  # type: ignore[method-assign]  # noqa: SLF001
            "REG-100001"
        }
    )
    pipeline._verify_live_sources = (  # type: ignore[method-assign]  # noqa: SLF001
        lambda _events: calls.append("verify")
    )

    version = asyncio.run(pipeline.process_pending())

    assert version == "build-44444444444444444444"
    expected_calls = ["apply", "verify", "stage", "prepare", "verify", "activate"]
    if fail_completion_once:
        expected_calls.append("recover")
    assert calls == expected_calls
    assert state.completed == (["evt_staged_publish"], version)


def test_native_llmwiki_adapter_rejects_symlinked_canonical_file(tmp_path: Path) -> None:
    fixture = _create_native_fixture(tmp_path)
    build = fixture.adapter.current_native_build()
    assert build is not None
    canonical = build.build_dir / "docs/regulations/REG-100001.md"
    target = tmp_path / "outside.md"
    target.write_text(canonical.read_text(encoding="utf-8"), encoding="utf-8")
    canonical.unlink()
    canonical.symlink_to(target)

    try:
        fixture.adapter.materialize(build)
    except LLMWikiCompatibilityError as error:
        assert "symlink" in str(error)
    else:
        raise AssertionError("symlinked native canonical file was accepted")


def test_native_pipeline_updates_normalized_body_source_hash_and_revision(
    tmp_path: Path,
) -> None:
    fixture = _create_native_fixture(tmp_path)
    pipeline = _build_pipeline(fixture, tmp_path)

    changed_source = fixture.source_path.read_text(encoding="utf-8").replace("1년", "3년")
    fixture.source_path.write_text(changed_source, encoding="utf-8", newline="\n")
    after_sha256 = _sha256(changed_source.encode())
    event = _event(
        fixture,
        event_id="evt_test",
        before_sha256=fixture.before_sha256,
        after_sha256=after_sha256,
        expected_text="1년",
        replacement_text="3년",
    )

    assert pipeline._apply_inputs([event]) == {"REG-100001"}  # noqa: SLF001
    assert pipeline._apply_inputs([event]) == {"REG-100001"}  # noqa: SLF001

    text = fixture.input_path.read_text(encoding="utf-8")
    lines = text.split("---", maxsplit=2)
    metadata = yaml.safe_load(lines[1])
    assert metadata["revision"] == "2"
    assert metadata["source"]["sha256"] == after_sha256
    assert "개인정보는 3년간 보관한다." in lines[2]


def test_native_pipeline_updates_server_v2_single_fragment_idempotently(
    tmp_path: Path,
) -> None:
    fixture = _create_native_fixture(tmp_path)
    _use_server_v2_ingest(fixture)
    pipeline = _build_pipeline(fixture, tmp_path)

    changed_source = fixture.source_path.read_text(encoding="utf-8").replace("1년", "3년")
    fixture.source_path.write_text(changed_source, encoding="utf-8", newline="\n")
    after_sha256 = _sha256(changed_source.encode())
    event = _event(
        fixture,
        event_id="evt_server_v2",
        before_sha256=fixture.before_sha256,
        after_sha256=after_sha256,
        expected_text="1년",
        replacement_text="3년",
    )

    assert pipeline._apply_inputs([event]) == {"REG-100001"}  # noqa: SLF001
    assert pipeline._apply_inputs([event]) == {"REG-100001"}  # noqa: SLF001

    text = fixture.input_path.read_text(encoding="utf-8")
    metadata = yaml.safe_load(text.split("---", maxsplit=2)[1])
    assert metadata["revision"] == "2"
    assert metadata["source"]["sha256"] == after_sha256
    assert "개인정보는 3년간 보관한다." in text


def test_native_pipeline_renders_v2_body_with_llmwiki_v1_frontmatter(tmp_path: Path) -> None:
    fixture = _create_native_fixture(tmp_path)
    pipeline = _build_pipeline(fixture, tmp_path)
    changed_source = fixture.source_path.read_text(encoding="utf-8").replace("1년", "3년")
    fixture.source_path.write_text(changed_source, encoding="utf-8", newline="\n")
    after_sha256 = _sha256(changed_source.encode())
    event = _event(
        fixture,
        event_id="evt_v2_frontmatter",
        before_sha256=fixture.before_sha256,
        after_sha256=after_sha256,
        expected_text="1년",
        replacement_text="3년",
    )
    converted_body = "# 원본 파일 제목\n\n## 보관 기간\n\n개인정보는 3년간 보관한다.\n"

    class FakeDoc2Md:
        def convert(self, *_args: object, **_kwargs: object) -> ConversionResult:
            return ConversionResult(
                canonical_markdown=(
                    "---\nschema_version: 1.1.0\ncanonical_sha256: ignored\n"
                    "converter_version: 0.2.0\n---\n" + converted_body
                ),
                body=converted_body,
                format="md",
                converter="doc2md:0.2.0:markitdown",
                warnings=(),
                cached=False,
                source_sha256=after_sha256,
                canonical_sha256=_sha256(converted_body.encode()),
                converter_version="0.2.0",
            )

    pipeline._doc2md = FakeDoc2Md()  # type: ignore[assignment]  # noqa: SLF001

    assert pipeline._apply_inputs([event]) == {"REG-100001"}  # noqa: SLF001

    text = fixture.input_path.read_text(encoding="utf-8")
    metadata = yaml.safe_load(text.split("---", maxsplit=2)[1])
    assert metadata["schema_version"] == "1.0.0"
    assert metadata["revision"] == "2"
    assert metadata["source"]["sha256"] == after_sha256
    assert "canonical_sha256" not in metadata
    assert "converter_version" not in metadata
    assert "# 개인정보 처리 규정" in text
    assert "# 원본 파일 제목" not in text
    assert "개인정보는 3년간 보관한다." in text


@pytest.mark.parametrize(
    "converted_body",
    [
        "본문만 있다.\n",
        "서문이 먼저다.\n\n# 뒤늦은 제목\n",
        "# 첫 제목\n\n# 두 번째 제목\n",
    ],
    ids=["missing", "not-leading", "duplicate"],
)
def test_native_pipeline_rejects_doc2md_body_without_one_leading_h1(
    tmp_path: Path,
    converted_body: str,
) -> None:
    fixture = _create_native_fixture(tmp_path)
    pipeline = _build_pipeline(fixture, tmp_path)
    changed_source = fixture.source_path.read_text(encoding="utf-8").replace("1년", "3년")
    fixture.source_path.write_text(changed_source, encoding="utf-8", newline="\n")
    after_sha256 = _sha256(changed_source.encode())
    event = _event(
        fixture,
        event_id="evt_invalid_h1",
        before_sha256=fixture.before_sha256,
        after_sha256=after_sha256,
        expected_text="1년",
        replacement_text="3년",
    )

    class FakeDoc2Md:
        def convert(self, *_args: object, **_kwargs: object) -> ConversionResult:
            return ConversionResult(
                canonical_markdown=converted_body,
                body=converted_body,
                format="md",
                converter="doc2md:0.2.0:markitdown",
                warnings=(),
                cached=False,
                source_sha256=after_sha256,
                canonical_sha256=_sha256(converted_body.encode()),
                converter_version="0.2.0",
            )

    pipeline._doc2md = FakeDoc2Md()  # type: ignore[assignment]  # noqa: SLF001

    with pytest.raises(LLMWikiCompatibilityError, match="exactly one leading H1"):
        pipeline._apply_inputs([event])  # noqa: SLF001


def test_native_pipeline_rejects_unapproved_normalized_input_content(tmp_path: Path) -> None:
    fixture = _create_native_fixture(tmp_path)
    pipeline = _build_pipeline(fixture, tmp_path)
    fixture.input_path.write_text(
        fixture.input_path.read_text(encoding="utf-8").replace(
            "## 보관 기간",
            "UNAPPROVED-CONTENT\n\n## 보관 기간",
        ),
        encoding="utf-8",
        newline="\n",
    )
    changed_source = fixture.source_path.read_text(encoding="utf-8").replace("1년", "3년")
    fixture.source_path.write_text(changed_source, encoding="utf-8", newline="\n")
    event = _event(
        fixture,
        event_id="evt_unapproved",
        before_sha256=fixture.before_sha256,
        after_sha256=_sha256(changed_source.encode()),
        expected_text="1년",
        replacement_text="3년",
    )

    with pytest.raises(LLMWikiCompatibilityError, match="unapproved change"):
        pipeline._apply_inputs([event])  # noqa: SLF001


def test_native_pipeline_rejects_live_source_hash_race(tmp_path: Path) -> None:
    fixture = _create_native_fixture(tmp_path)
    pipeline = _build_pipeline(fixture, tmp_path)
    approved_source = fixture.source_path.read_text(encoding="utf-8").replace("1년", "3년")
    event = _event(
        fixture,
        event_id="evt_source_race",
        before_sha256=fixture.before_sha256,
        after_sha256=_sha256(approved_source.encode()),
        expected_text="1년",
        replacement_text="3년",
    )
    fixture.source_path.write_text(
        approved_source.replace("3년", "9년"),
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(LLMWikiCompatibilityError, match="live source changed"):
        pipeline._apply_inputs([event])  # noqa: SLF001

    fixture.source_path.write_text(approved_source, encoding="utf-8", newline="\n")
    assert pipeline._apply_inputs([event]) == {"REG-100001"}  # noqa: SLF001
    fixture.source_path.write_text(
        approved_source.replace("3년", "9년"),
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(LLMWikiCompatibilityError, match="live source changed"):
        pipeline._verify_live_sources([event])  # noqa: SLF001


def test_native_pipeline_restores_active_input_after_failed_publish_undo(
    tmp_path: Path,
) -> None:
    fixture = _create_native_fixture(tmp_path)
    _use_server_v2_ingest(fixture)
    pipeline = _build_pipeline(fixture, tmp_path)
    original_source = fixture.source_path.read_text(encoding="utf-8")
    changed_source = original_source.replace("1년", "3년")
    changed_sha256 = _sha256(changed_source.encode())
    fixture.source_path.write_text(changed_source, encoding="utf-8", newline="\n")
    forward = _event(
        fixture,
        event_id="evt_forward",
        before_sha256=fixture.before_sha256,
        after_sha256=changed_sha256,
        expected_text="1년",
        replacement_text="3년",
    )
    assert pipeline._apply_inputs([forward]) == {"REG-100001"}  # noqa: SLF001

    fixture.source_path.write_text(original_source, encoding="utf-8", newline="\n")
    undo = _event(
        fixture,
        event_id="evt_undo",
        before_sha256=changed_sha256,
        after_sha256=fixture.before_sha256,
        expected_text="3년",
        replacement_text="1년",
    )

    assert pipeline._apply_inputs([undo]) == set()  # noqa: SLF001
    restored = fixture.input_path.read_text(encoding="utf-8")
    metadata = yaml.safe_load(restored.split("---", maxsplit=2)[1])
    assert metadata["revision"] == "1"
    assert metadata["source"]["sha256"] == fixture.before_sha256
    assert "개인정보는 1년간 보관한다." in restored
    assert "개인정보는 3년간 보관한다." not in restored
