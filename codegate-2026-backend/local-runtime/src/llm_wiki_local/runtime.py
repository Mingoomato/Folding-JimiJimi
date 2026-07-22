from __future__ import annotations

import hashlib
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from llm_wiki_local.config import RuntimeSettings
from llm_wiki_local.errors import (
    BuildActivationError,
    ConversionError,
    EventConflictError,
    SourceAccessError,
)
from llm_wiki_local.input_snapshots import InputSnapshots
from llm_wiki_local.models import (
    BuildOutcome,
    EventSubmission,
    SourceEvent,
    SourceEventType,
    SourceRecord,
)
from llm_wiki_local.store import StateStore

SUPPORTED_EXTENSIONS = {
    ".csv",
    ".docx",
    ".htm",
    ".html",
    ".hwp",
    ".hwpx",
    ".md",
    ".pdf",
    ".pptx",
    ".txt",
    ".xlsx",
}
DOC_TYPE_PREFIX = {
    "regulation": "REG",
    "policy": "POL",
    "procedure": "PRO",
    "manual": "MAN",
    "guide": "GUI",
    "specification": "SPEC",
    "contract": "CON",
    "report": "REP",
    "meeting_note": "MIN",
    "general": "GEN",
}
DOC_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$")
CONVERTER_METADATA_KEYS = {
    "id",
    "title",
    "doc_type",
    "language",
    "revision",
    "status",
    "official_number",
    "authority_level",
    "issuing_org",
    "issued_on",
    "effective_from",
    "effective_to",
    "uri",
    "access",
    "tags",
    "aliases",
}


class Converter(Protocol):
    def convert(
        self,
        path: Path,
        metadata: dict[str, Any],
        *,
        job_id: str,
        progress: Any | None = None,
    ) -> dict[str, Any]: ...


class Builder(Protocol):
    def build(
        self,
        input_dir: Path,
        *,
        doc_id: str,
        deleted: bool,
    ) -> BuildOutcome: ...


class LocalWikiRuntime:
    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        store: StateStore,
        converter: Converter,
        snapshots: InputSnapshots,
        builder: Builder,
    ):
        self.settings = settings
        self.store = store
        self.converter = converter
        self.snapshots = snapshots
        self.builder = builder
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm-wiki-local")
        self._started = False
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        for job_id in self.store.pending_job_ids():
            self._schedule(job_id)

    def shutdown(self) -> None:
        with self._lock:
            self._started = False
        self._executor.shutdown(wait=True, cancel_futures=False)

    def submit_event(self, event: SourceEvent) -> EventSubmission:
        existing_job_id = self.store.job_for_event(event.event_id)
        if existing_job_id is not None:
            existing = self.store.job(existing_job_id)
            return EventSubmission(
                duplicate=True,
                event_id=event.event_id,
                job_id=existing_job_id,
                status=str(existing["status"] if existing else "queued"),
            )

        cache_path: Path | None = None
        observed_sha256: str | None = None
        if event.event_type in {SourceEventType.CREATED, SourceEventType.UPDATED}:
            cache_path, observed_sha256 = self._cache_source(event)

        job_id = uuid.uuid4().hex
        accepted_job_id, created = self.store.accept_event(
            event,
            job_id=job_id,
            cache_path=cache_path,
            observed_sha256=observed_sha256,
        )
        if created:
            with self._lock:
                started = self._started
            if started:
                self._schedule(accepted_job_id)
        job = self.store.job(accepted_job_id)
        return EventSubmission(
            duplicate=not created,
            event_id=event.event_id,
            job_id=accepted_job_id,
            status=str(job["status"] if job else "queued"),
        )

    def process_job(self, job_id: str, *, raise_errors: bool = False) -> None:
        candidate: Path | None = None
        try:
            self.store.update_job(
                job_id,
                status="running",
                phase="validating-event",
                message="이벤트와 현재 원본 상태 확인 중",
            )
            event, cache_path, observed_sha256 = self.store.event_for_job(job_id)
            source = self.store.source(event.source_id)
            no_op = self._no_op_reason(event, source, observed_sha256)
            if no_op is not None:
                if source is not None and event.sequence > source.last_sequence:
                    self.store.advance_source_sequence(source.source_id, event.sequence)
                self.store.update_job(
                    job_id,
                    status="succeeded",
                    phase="done",
                    message=no_op,
                )
                return

            self._validate_transition(event, source, observed_sha256)
            active_input_dir = self._active_input_dir()
            candidate = self.snapshots.create_candidate(job_id, active_input_dir)
            self.store.update_job(
                job_id,
                phase="preparing-input",
                message="후보 Markdown 입력 스냅샷 준비 중",
                candidate_input_dir=candidate,
            )

            if event.event_type == SourceEventType.DELETED:
                if source is None:
                    raise EventConflictError(f"unknown source_id: {event.source_id}")
                self.snapshots.remove_fragments(
                    candidate,
                    source.fragment_files,
                    require_all=True,
                )
                doc_id = source.doc_id
                prepared_metadata = source.metadata
                fragment_files: list[str] = []
            else:
                if cache_path is None or observed_sha256 is None:
                    raise ConversionError("accepted event has no cached source file")
                doc_id = self._document_id(event, source)
                doc_owner = self.store.source_by_doc_id(doc_id)
                if doc_owner is not None and doc_owner.source_id != event.source_id:
                    raise EventConflictError(f"doc_id is already registered: {doc_id}")
                path_owner = self.store.active_source_by_relative_path(event.relative_path)
                if path_owner is not None and path_owner.source_id != event.source_id:
                    raise EventConflictError(
                        f"relative_path is already registered: {event.relative_path}"
                    )
                converter_metadata = self._converter_metadata(event, source, doc_id)
                self.store.update_job(
                    job_id,
                    phase="converting",
                    message="원본 문서를 Markdown으로 변환 중",
                )
                result = self.converter.convert(
                    cache_path,
                    converter_metadata,
                    job_id=job_id,
                    progress=lambda phase, message: self.store.update_job(
                        job_id,
                        phase=f"converting:{phase}",
                        message=message or "문서 변환 중",
                    ),
                )
                prepared = self.snapshots.replace_document(
                    candidate,
                    old_fragment_files=source.fragment_files if source else [],
                    result=result,
                    doc_id=doc_id,
                    relative_path=event.relative_path,
                    source_sha256=observed_sha256,
                    requested_metadata=converter_metadata,
                )
                prepared_metadata = prepared.metadata
                fragment_files = prepared.fragment_files

            deleted = event.event_type == SourceEventType.DELETED
            self.store.update_job(
                job_id,
                phase="building",
                message="불변 LLM Wiki 빌드 생성 중",
            )
            outcome = self.builder.build(candidate, doc_id=doc_id, deleted=deleted)
            if not outcome.activated:
                raise BuildActivationError(
                    f"build {outcome.build_id} was not activated; current wiki is unchanged"
                )

            promoted = self.snapshots.promote(candidate, outcome.build_id)
            candidate = None
            self.store.set_state("active_input_dir", str(promoted))
            self.store.set_state("active_build_id", outcome.build_id)
            if deleted:
                if source is None:
                    raise EventConflictError(f"unknown source_id: {event.source_id}")
                self.store.tombstone_source(source, sequence=event.sequence)
            else:
                if cache_path is None or observed_sha256 is None:
                    raise ConversionError("accepted event has no cached source file")
                self.store.activate_source(
                    source_id=event.source_id,
                    doc_id=doc_id,
                    relative_path=event.relative_path,
                    active_sha256=observed_sha256,
                    sequence=event.sequence,
                    metadata=prepared_metadata,
                    fragment_files=fragment_files,
                    cache_path=cache_path,
                )
            self.store.update_job(
                job_id,
                status="succeeded",
                phase="done",
                message="새 위키 빌드 활성화 완료",
                build_id=outcome.build_id,
            )
        except Exception as exc:
            self.snapshots.discard(candidate)
            self.store.update_job(
                job_id,
                status="failed",
                phase="failed",
                message=str(exc),
                error_code=type(exc).__name__,
            )
            if raise_errors:
                raise

    def _schedule(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._in_flight:
                return
            self._in_flight.add(job_id)

        def run() -> None:
            try:
                self.process_job(job_id)
            finally:
                with self._lock:
                    self._in_flight.discard(job_id)

        self._executor.submit(run)

    def _cache_source(self, event: SourceEvent) -> tuple[Path, str]:
        if event.source_path is None:
            raise SourceAccessError("source_path is required")
        original = Path(event.source_path).expanduser()
        if original.is_symlink():
            raise SourceAccessError(f"symbolic links are not allowed: {original}")
        try:
            resolved = original.resolve(strict=True)
        except FileNotFoundError as exc:
            raise SourceAccessError(f"source file not found: {original}") from exc
        if not resolved.is_file():
            raise SourceAccessError(f"source path is not a regular file: {resolved}")
        if resolved.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise SourceAccessError(f"unsupported source extension: {resolved.suffix}")
        relative_suffix = Path(event.relative_path).suffix.lower()
        if relative_suffix != resolved.suffix.lower():
            raise SourceAccessError(
                "source_path and relative_path must use the same file extension"
            )
        if resolved.stat().st_size > self.settings.max_source_bytes:
            raise SourceAccessError(
                f"source file exceeds {self.settings.max_source_bytes} bytes: {resolved}"
            )
        if self.settings.allowed_source_roots and not any(
            resolved.is_relative_to(root.resolve()) for root in self.settings.allowed_source_roots
        ):
            raise SourceAccessError(f"source path is outside allowed roots: {resolved}")

        incoming = self.settings.source_cache_dir / ".incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        safe_event_id = re.sub(r"[^A-Za-z0-9._-]", "_", event.event_id)
        temporary = incoming / f"{safe_event_id}.{uuid.uuid4().hex}.tmp"
        digest = hashlib.sha256()
        copied_bytes = 0
        try:
            with resolved.open("rb") as source, temporary.open("xb") as destination:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    copied_bytes += len(block)
                    if copied_bytes > self.settings.max_source_bytes:
                        raise SourceAccessError(
                            f"source file grew beyond {self.settings.max_source_bytes} bytes"
                        )
                    digest.update(block)
                    destination.write(block)
                destination.flush()
                os.fsync(destination.fileno())
            sha256 = digest.hexdigest()
            if event.source_sha256 and sha256 != event.source_sha256.lower():
                raise EventConflictError(
                    f"source_sha256 mismatch: expected {event.source_sha256.lower()}, got {sha256}"
                )
            target_dir = self.settings.source_cache_dir / sha256
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / Path(event.relative_path).name
            if target.exists():
                temporary.unlink()
            else:
                os.replace(temporary, target)
            return target, sha256
        except BaseException:
            if temporary.exists():
                temporary.unlink()
            raise

    @staticmethod
    def _no_op_reason(
        event: SourceEvent,
        source: SourceRecord | None,
        observed_sha256: str | None,
    ) -> str | None:
        if source is not None and event.sequence <= source.last_sequence:
            return "이미 반영된 sequence보다 오래된 이벤트이므로 무시"
        if (
            event.event_type == SourceEventType.DELETED
            and source is not None
            and source.status == "deleted"
        ):
            return "이미 삭제된 문서이므로 변경 없음"
        if (
            event.event_type == SourceEventType.CREATED
            and source is not None
            and source.status == "active"
            and source.active_sha256 == observed_sha256
        ):
            return "동일한 원본이 이미 활성화되어 변경 없음"
        if (
            event.event_type == SourceEventType.UPDATED
            and source is not None
            and source.status == "active"
            and source.active_sha256 == observed_sha256
        ):
            return "수정 후 해시가 현재 원본과 같아 변경 없음"
        return None

    @staticmethod
    def _validate_transition(
        event: SourceEvent,
        source: SourceRecord | None,
        observed_sha256: str | None,
    ) -> None:
        if event.event_type == SourceEventType.CREATED:
            if source is not None and source.status == "active":
                raise EventConflictError(
                    f"created event targets an active source: {event.source_id}"
                )
            if observed_sha256 is None:
                raise EventConflictError("created event has no observed SHA-256")
            return
        if source is None:
            raise EventConflictError(f"unknown source_id: {event.source_id}")
        if source.status != "active":
            raise EventConflictError(f"source is not active: {event.source_id}")
        if event.base_source_sha256 != source.active_sha256:
            raise EventConflictError(
                "base_source_sha256 does not match the active source version"
            )
        if event.event_type == SourceEventType.UPDATED and observed_sha256 is None:
            raise EventConflictError("updated event has no observed SHA-256")

    @staticmethod
    def _document_id(event: SourceEvent, source: SourceRecord | None) -> str:
        if source is not None:
            requested = event.metadata.id
            if requested is not None and requested != source.doc_id:
                raise EventConflictError(
                    f"update cannot change doc_id from {source.doc_id} to {requested}"
                )
            return source.doc_id
        if event.metadata.id:
            doc_id = event.metadata.id
        else:
            prefix = DOC_TYPE_PREFIX.get(event.metadata.doc_type, "GEN")
            suffix = hashlib.sha256(event.source_id.encode("utf-8")).hexdigest()[:12].upper()
            doc_id = f"{prefix}-{suffix}"
        if not DOC_ID_RE.fullmatch(doc_id):
            raise EventConflictError(f"invalid doc_id: {doc_id}")
        return doc_id

    @staticmethod
    def _converter_metadata(
        event: SourceEvent,
        source: SourceRecord | None,
        doc_id: str,
    ) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        if source is not None:
            merged.update(
                {
                    key: value
                    for key, value in source.metadata.items()
                    if key in CONVERTER_METADATA_KEYS
                }
            )
        merged.update(event.metadata.model_dump(exclude_unset=True))
        merged = {key: value for key, value in merged.items() if key in CONVERTER_METADATA_KEYS}
        merged["id"] = doc_id
        merged.setdefault("doc_type", "general")
        merged.setdefault("revision", "1")
        merged.setdefault("status", "active")
        merged.setdefault("access", "internal")
        merged["uri"] = event.metadata.uri or f"source://{event.relative_path}"
        return merged

    def _active_input_dir(self) -> Path | None:
        value = self.store.get_state("active_input_dir")
        if value:
            return Path(value)
        current_pointer = (
            self.settings.storage_root
            / "tenants"
            / self.settings.tenant_id
            / "wikis"
            / self.settings.wiki_id
            / "current.json"
        )
        if current_pointer.is_file() and not any(
            self.settings.bootstrap_input_dir.glob("*.md")
        ):
            raise EventConflictError(
                "an active wiki already exists but no bootstrap Markdown input was configured"
            )
        return None
