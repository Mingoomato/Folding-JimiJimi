from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest
from wiki_builder.config import WikiScope, load_config, scoped_config
from wiki_builder.markdown import FRONT_MATTER_ORDER, load_documents

from llm_wiki_local.errors import BuildActivationError, EventConflictError
from llm_wiki_local.input_snapshots import BUILDER_FRONT_MATTER_FIELDS
from llm_wiki_local.models import ConversionMetadata, SourceEvent


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source(settings, value: str) -> Path:
    path = settings.allowed_source_roots[0] / "security.txt"
    path.write_text(value, encoding="utf-8")
    return path


def test_created_updated_deleted_flow(runtime, settings) -> None:
    source_path = _source(settings, "첫 번째 본문")
    first_sha = _sha(source_path)
    created = SourceEvent(
        event_id="event-create",
        event_type="created",
        source_id="source-security",
        sequence=1,
        relative_path="regulations/security.txt",
        source_path=str(source_path),
        source_sha256=first_sha,
        metadata=ConversionMetadata(
            id="REG-000001",
            title="정보보안 규정",
            doc_type="regulation",
            revision="1",
        ),
    )

    create_submission = runtime.submit_event(created)
    runtime.process_job(create_submission.job_id, raise_errors=True)

    created_job = runtime.store.job(create_submission.job_id)
    record = runtime.store.source("source-security")
    assert created_job["status"] == "succeeded"
    assert record is not None
    assert record.doc_id == "REG-000001"
    assert record.active_sha256 == first_sha
    assert record.fragment_files == ["REG-000001_001.md"]
    active_one = Path(runtime.store.get_state("active_input_dir"))
    markdown_one = (active_one / "REG-000001_001.md").read_text(encoding="utf-8")
    assert "첫 번째 본문" in markdown_one
    assert "chunk_no: REG-000001_001" in markdown_one

    source_path.write_text("두 번째 본문", encoding="utf-8")
    second_sha = _sha(source_path)
    updated = SourceEvent(
        event_id="event-update",
        event_type="updated",
        source_id="source-security",
        sequence=2,
        relative_path="regulations/security.txt",
        source_path=str(source_path),
        source_sha256=second_sha,
        base_source_sha256=first_sha,
        metadata=ConversionMetadata(revision="2"),
    )
    update_submission = runtime.submit_event(updated)
    runtime.process_job(update_submission.job_id, raise_errors=True)

    record = runtime.store.source("source-security")
    assert record is not None
    assert record.doc_id == "REG-000001"
    assert record.active_sha256 == second_sha
    assert record.source_version == 2
    assert runtime.fake_converter.calls[-1]["metadata"]["id"] == "REG-000001"
    assert runtime.fake_converter.calls[-1]["metadata"]["revision"] == "2"
    active_two = Path(runtime.store.get_state("active_input_dir"))
    assert active_two != active_one
    assert "두 번째 본문" in (active_two / "REG-000001_001.md").read_text(encoding="utf-8")
    assert "첫 번째 본문" in markdown_one

    deleted = SourceEvent(
        event_id="event-delete",
        event_type="deleted",
        source_id="source-security",
        sequence=3,
        relative_path="regulations/security.txt",
        base_source_sha256=second_sha,
    )
    delete_submission = runtime.submit_event(deleted)
    runtime.process_job(delete_submission.job_id, raise_errors=True)

    record = runtime.store.source("source-security")
    assert record is not None
    assert record.status == "deleted"
    assert record.fragment_files == []
    active_three = Path(runtime.store.get_state("active_input_dir"))
    assert list(active_three.glob("*.md")) == []
    assert runtime.fake_builder.calls[-1]["deleted"] is True
    assert len(runtime.fake_converter.calls) == 2


def test_duplicate_event_returns_original_job(runtime, settings) -> None:
    source_path = _source(settings, "본문")
    event = SourceEvent(
        event_id="same-event",
        event_type="created",
        source_id="source-1",
        sequence=1,
        relative_path="document.txt",
        source_path=str(source_path),
    )

    first = runtime.submit_event(event)
    second = runtime.submit_event(event)

    assert second.duplicate is True
    assert second.job_id == first.job_id
    runtime.process_job(first.job_id, raise_errors=True)
    assert len(runtime.fake_converter.calls) == 1


def test_failed_candidate_does_not_replace_active_input(runtime, settings) -> None:
    source_path = _source(settings, "활성 본문")
    first_sha = _sha(source_path)
    created = SourceEvent(
        event_id="create",
        event_type="created",
        source_id="source-1",
        sequence=1,
        relative_path="document.txt",
        source_path=str(source_path),
        metadata=ConversionMetadata(id="GEN-000001", title="문서"),
    )
    create_job = runtime.submit_event(created).job_id
    runtime.process_job(create_job, raise_errors=True)
    active_before = runtime.store.get_state("active_input_dir")

    source_path.write_text("실패할 수정 본문", encoding="utf-8")
    runtime.fake_builder.activate = False
    updated = SourceEvent(
        event_id="update",
        event_type="updated",
        source_id="source-1",
        sequence=2,
        relative_path="document.txt",
        source_path=str(source_path),
        base_source_sha256=first_sha,
    )
    update_job = runtime.submit_event(updated).job_id

    with pytest.raises(BuildActivationError):
        runtime.process_job(update_job, raise_errors=True)

    record = runtime.store.source("source-1")
    assert record is not None
    assert record.active_sha256 == first_sha
    assert runtime.store.get_state("active_input_dir") == active_before
    assert runtime.store.job(update_job)["status"] == "failed"
    assert not list(settings.input_snapshots_dir.glob(".candidate-*"))


def test_stale_update_is_rejected(runtime, settings) -> None:
    source_path = _source(settings, "기존 본문")
    created = SourceEvent(
        event_id="create",
        event_type="created",
        source_id="source-1",
        sequence=1,
        relative_path="document.txt",
        source_path=str(source_path),
    )
    create_job = runtime.submit_event(created).job_id
    runtime.process_job(create_job, raise_errors=True)

    source_path.write_text("새 본문", encoding="utf-8")
    updated = SourceEvent(
        event_id="stale-update",
        event_type="updated",
        source_id="source-1",
        sequence=2,
        relative_path="document.txt",
        source_path=str(source_path),
        base_source_sha256="f" * 64,
    )
    update_job = runtime.submit_event(updated).job_id

    with pytest.raises(EventConflictError, match="base_source_sha256"):
        runtime.process_job(update_job, raise_errors=True)
    assert runtime.store.job(update_job)["status"] == "failed"


def test_generated_markdown_satisfies_builder_input_contract(runtime, settings) -> None:
    assert frozenset(FRONT_MATTER_ORDER) == BUILDER_FRONT_MATTER_FIELDS
    source_path = _source(settings, "계약 검증 본문")
    event = SourceEvent(
        event_id="contract-create",
        event_type="created",
        source_id="contract-source",
        sequence=1,
        relative_path="regulations/contract.txt",
        source_path=str(source_path),
        metadata=ConversionMetadata(
            id="REG-009999",
            title="입력 계약 검증 규정",
            doc_type="regulation",
            authority_level="regulation",
        ),
    )
    job_id = runtime.submit_event(event).job_id
    runtime.process_job(job_id, raise_errors=True)
    active_input = Path(runtime.store.get_state("active_input_dir"))

    repository_root = Path(__file__).resolve().parents[2]
    config = load_config(repository_root / "wiki-builder" / "config" / "wiki.yaml")
    scope = WikiScope(
        tenant_id="test",
        wiki_id="contract",
        input_dir=active_input,
        storage_root=settings.storage_root,
        synthetic_corpus=True,
        actor_id="test",
    )
    documents = load_documents(scoped_config(config, scope))

    assert len(documents) == 1
    assert documents[0].doc_id == "REG-009999"
    assert documents[0].chunking_mode == "source-fragments"
    assert documents[0].source_fragments[0].input_relative_path == "REG-009999_001.md"
    rendered = (active_input / "REG-009999_001.md").read_text(encoding="utf-8")
    assert "canonical_sha256" not in rendered
    assert "converter_version" not in rendered


def test_running_job_is_recovered_when_runtime_starts(runtime, settings) -> None:
    source_path = _source(settings, "재시작 복구 본문")
    event = SourceEvent(
        event_id="recover-event",
        event_type="created",
        source_id="recover-source",
        sequence=1,
        relative_path="recover.txt",
        source_path=str(source_path),
    )
    job_id = runtime.submit_event(event).job_id
    runtime.store.update_job(
        job_id,
        status="running",
        phase="converting",
        message="중단된 작업",
    )

    runtime.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if runtime.store.job(job_id)["status"] == "succeeded":
            break
        time.sleep(0.01)

    assert runtime.store.job(job_id)["status"] == "succeeded"


def test_existing_wiki_requires_bootstrap_markdown(runtime, settings) -> None:
    current = (
        settings.storage_root
        / "tenants"
        / settings.tenant_id
        / "wikis"
        / settings.wiki_id
        / "current.json"
    )
    current.parent.mkdir(parents=True)
    current.write_text("{}", encoding="utf-8")
    source_path = _source(settings, "기존 위키 보호")
    event = SourceEvent(
        event_id="protected-create",
        event_type="created",
        source_id="protected-source",
        sequence=1,
        relative_path="protected.txt",
        source_path=str(source_path),
    )
    job_id = runtime.submit_event(event).job_id

    with pytest.raises(EventConflictError, match="bootstrap Markdown"):
        runtime.process_job(job_id, raise_errors=True)

    assert runtime.fake_builder.calls == []


def test_two_sources_cannot_claim_the_same_doc_id(runtime, settings) -> None:
    first_path = _source(settings, "첫 문서")
    first = SourceEvent(
        event_id="first-create",
        event_type="created",
        source_id="first-source",
        sequence=1,
        relative_path="first.txt",
        source_path=str(first_path),
        metadata=ConversionMetadata(id="GEN-000777"),
    )
    first_job = runtime.submit_event(first).job_id
    runtime.process_job(first_job, raise_errors=True)

    second_path = settings.allowed_source_roots[0] / "second.txt"
    second_path.write_text("두 번째 문서", encoding="utf-8")
    second = SourceEvent(
        event_id="second-create",
        event_type="created",
        source_id="second-source",
        sequence=1,
        relative_path="second.txt",
        source_path=str(second_path),
        metadata=ConversionMetadata(id="GEN-000777"),
    )
    second_job = runtime.submit_event(second).job_id

    with pytest.raises(EventConflictError, match="doc_id is already registered"):
        runtime.process_job(second_job, raise_errors=True)
