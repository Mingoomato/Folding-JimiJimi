from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from codegate_api.documents.models import DocumentContentChangedV2
from codegate_api.files.atomic import SafeSourceFileStore
from codegate_api.integrations.doc2md import ConversionSection, Doc2MdClient, Doc2MdError
from codegate_api.knowledge.catalog import KnowledgeCatalog, KnowledgeCatalogError
from codegate_api.knowledge.evidence import build_section_index
from codegate_api.knowledge.repository import KnowledgePackageError
from codegate_api.knowledge.schemas import ManifestEntry
from codegate_api.models import ReplaceExactOperation
from codegate_api.state.store import PendingEvent, StateStore


class SyncPipelineError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ContentChangedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0.0"
    event_id: str
    execution_id: str
    document_id: str
    source_uri: str
    before_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_version_id: str
    operation: ReplaceExactOperation


class LocalKnowledgePipeline:
    """Local, replaceable converter/index/graph adapter with atomic publication."""

    def __init__(
        self,
        *,
        catalog: KnowledgeCatalog,
        file_store: SafeSourceFileStore,
        state_store: StateStore,
        doc2md: Doc2MdClient | None = None,
        fail_stage: str | None = None,
    ) -> None:
        self._catalog = catalog
        self._files = file_store
        self._state = state_store
        self._doc2md = doc2md
        self._lock = asyncio.Lock()
        self._fail_stage = fail_stage

    async def process_pending(self) -> str:
        async with self._lock:
            current_batch: list[PendingEvent] = []
            candidate: Path | None = None
            failure_code = "KNOWLEDGE_SYNC_FAILED"
            failure_message = "지식 동기화가 완료되지 않았습니다. 재시도할 수 있습니다."
            failure_retryable = True
            try:
                for _ in range(8):
                    current_batch = self._state.pending_events()
                    if not current_batch:
                        return self._catalog.snapshot().version
                    event_ids = [event.event_id for event in current_batch]
                    self._state.begin_event_batch(event_ids)
                    parent_version = self._catalog.snapshot().version
                    candidate = self._catalog.stage(parent_version)
                    candidate_version = _candidate_version(parent_version, event_ids)
                    changed = await self._build_candidate(
                        candidate,
                        parent_version=parent_version,
                        candidate_version=candidate_version,
                        events=current_batch,
                    )

                    latest_ids = [event.event_id for event in self._state.pending_events()]
                    if latest_ids != event_ids:
                        self._catalog.discard(candidate)
                        candidate = None
                        await asyncio.sleep(0)
                        continue
                    if not changed:
                        self._catalog.discard(candidate)
                        candidate = None
                        self._state.complete_event_batch(event_ids, parent_version)
                        return parent_version

                    self._state.record_sync_task(
                        event_ids,
                        stage="publish",
                        status="running",
                        output={"expected_parent_version": parent_version},
                    )
                    published = self._catalog.publish(
                        candidate,
                        expected_parent_version=parent_version,
                        candidate_version=candidate_version,
                    )
                    if not published:
                        self._state.record_sync_task(
                            event_ids,
                            stage="publish",
                            status="rebasing",
                        )
                        self._catalog.discard(candidate)
                        candidate = None
                        self._catalog.refresh()
                        await asyncio.sleep(0)
                        continue
                    candidate = None
                    self._state.record_sync_task(
                        event_ids,
                        stage="publish",
                        status="succeeded",
                        output={"graph_version": candidate_version},
                    )
                    self._state.record_knowledge_version(
                        version=candidate_version,
                        parent_version=parent_version,
                        event_ids=event_ids,
                        status="active",
                    )
                    self._state.complete_event_batch(event_ids, candidate_version)
                    return candidate_version
                raise SyncPipelineError(
                    "PUBLISH_CAS_EXHAUSTED",
                    "지식 버전 공개 충돌을 제한 횟수 안에 재조정하지 못했습니다.",
                )
            except Doc2MdError as error:
                failure_code = error.code
                failure_message = str(error)
                failure_retryable = error.retryable
                raise SyncPipelineError(
                    error.code,
                    str(error),
                    retryable=error.retryable,
                ) from error
            except (KnowledgeCatalogError, KnowledgePackageError, OSError, ValueError) as error:
                raise SyncPipelineError(failure_code, str(error)) from error
            except SyncPipelineError as error:
                failure_code = error.code
                failure_message = str(error)
                failure_retryable = error.retryable
                raise
            finally:
                if candidate is not None:
                    self._catalog.discard(candidate)
                if current_batch:
                    current_ids = [event.event_id for event in current_batch]
                    still_pending = {event.event_id for event in self._state.pending_events()}
                    failed_ids = [event_id for event_id in current_ids if event_id in still_pending]
                    if failed_ids:
                        self._state.fail_event_batch(
                            failed_ids,
                            code=failure_code,
                            message=failure_message,
                            retryable=failure_retryable,
                        )

    async def process_document_event(self, payload_data: dict[str, Any]) -> str:
        """Rebuild one complete document from its approved final source artifact."""

        payload = DocumentContentChangedV2.model_validate(payload_data)
        async with self._lock:
            for _ in range(4):
                parent_version = self._catalog.snapshot().version
                candidate = self._catalog.stage(parent_version)
                try:
                    changed = await asyncio.to_thread(
                        self._apply_document_v2,
                        candidate,
                        payload,
                    )
                    if not changed:
                        self._catalog.discard(candidate)
                        return parent_version
                    event_ids = [payload.event_id]
                    stage_results = await asyncio.gather(
                        asyncio.to_thread(self._build_fts, candidate),
                        asyncio.to_thread(self._build_vectors, candidate),
                        asyncio.to_thread(self._build_graph, candidate),
                        return_exceptions=True,
                    )
                    for result in stage_results:
                        if isinstance(result, BaseException):
                            raise result
                    candidate_version = _candidate_version(parent_version, event_ids)
                    await asyncio.to_thread(
                        self._finalize_candidate,
                        candidate,
                        parent_version,
                        candidate_version,
                        event_ids,
                    )
                    await asyncio.to_thread(self._catalog.validate_candidate, candidate)
                    if payload.after_sha256 is not None:
                        live = await asyncio.to_thread(self._files.snapshot, payload.source_uri)
                        if live.sha256 != payload.after_sha256:
                            raise SyncPipelineError(
                                "SOURCE_HASH_CONFLICT",
                                "source changed during document synchronization",
                            )
                    published = await asyncio.to_thread(
                        self._catalog.publish,
                        candidate,
                        expected_parent_version=parent_version,
                        candidate_version=candidate_version,
                    )
                    if not published:
                        self._catalog.discard(candidate)
                        self._catalog.refresh()
                        await asyncio.sleep(0)
                        continue
                    self._state.record_knowledge_version(
                        version=candidate_version,
                        parent_version=parent_version,
                        event_ids=event_ids,
                        status="active",
                    )
                    return candidate_version
                except BaseException:
                    self._catalog.discard(candidate)
                    raise
            raise SyncPipelineError(
                "PUBLISH_CAS_EXHAUSTED",
                "document synchronization could not publish after four rebases",
            )

    def _apply_document_v2(
        self,
        candidate: Path,
        payload: DocumentContentChangedV2,
    ) -> bool:
        manifest_path = candidate / "manifest.jsonl"
        chunks_path = candidate / "retrieval/chunks.jsonl"
        links_path = candidate / "retrieval/links.jsonl"
        manifests = _read_jsonl(manifest_path)
        chunks = _read_jsonl(chunks_path)
        links = _read_jsonl(links_path)
        manifest_by_id = {str(item["id"]): item for item in manifests}
        current = manifest_by_id.get(payload.document_id)

        if payload.change_kind == "recovery_remove":
            if current is None:
                return False
            if payload.before_sha256 != current["source"]["sha256"]:
                raise SyncPipelineError("EVENT_BASE_CONFLICT", "creation Undo base hash mismatch")
            manifests = [item for item in manifests if item["id"] != payload.document_id]
            chunks = [item for item in chunks if item["document_id"] != payload.document_id]
            links = [
                item
                for item in links
                if item["source_id"] != payload.document_id
                and item["target_id"] != payload.document_id
            ]
            canonical = candidate / current["canonical_path"]
            reference = candidate / f"references/{payload.document_id}.source.json"
            canonical.unlink(missing_ok=True)
            reference.unlink(missing_ok=True)
            _write_jsonl(manifest_path, manifests)
            _write_jsonl(chunks_path, chunks)
            _write_jsonl(links_path, links)
            return True

        if payload.after_sha256 is None:
            raise SyncPipelineError("EVENT_HASH_MISSING", "changed document has no after hash")
        source = self._files.snapshot(payload.source_uri)
        if source.sha256 != payload.after_sha256:
            raise SyncPipelineError("SOURCE_HASH_CONFLICT", "live source does not match event")
        if payload.change_kind == "update":
            if current is None:
                raise SyncPipelineError("UNKNOWN_DOCUMENT", "update references unknown document")
            if current["source"]["uri"] != payload.source_uri:
                raise SyncPipelineError("SOURCE_URI_CONFLICT", "update source URI differs")
            if current["source"]["sha256"] == payload.after_sha256:
                return False
            if current["source"]["sha256"] != payload.before_sha256:
                raise SyncPipelineError("EVENT_BASE_CONFLICT", "update base hash mismatch")
            revision = _next_revision(str(current["revision"]))
            manifest = current
        else:
            if current is not None:
                if current["source"]["sha256"] == payload.after_sha256:
                    return False
                raise SyncPipelineError(
                    "DOCUMENT_SET_CONFLICT", "create may add only one new document"
                )
            revision = "1"
            manifest = _new_manifest_v2(payload, revision)
            manifests.append(manifest)
            manifest_by_id[payload.document_id] = manifest

        file_version_id = "dfv_" + payload.after_sha256[:24]
        converted_body: str
        converted_sections: tuple[ConversionSection, ...]
        converter_name = "local-text-adapter"
        converter_version = "1.0.0"
        if self._doc2md is not None:
            converted_manifest = ManifestEntry.model_validate(manifest).model_copy(
                update={
                    "revision": revision,
                    "file_version_id": file_version_id,
                    "source": ManifestEntry.model_validate(manifest).source.model_copy(
                        update={"sha256": payload.after_sha256}
                    ),
                }
            )
            converted = self._doc2md.convert(
                converted_manifest,
                expected_source_sha256=payload.after_sha256,
            )
            converted_body = converted.body
            converted_sections = converted.sections
            converter_name = converted.converter
            converter_version = converted.converter_version or "unknown"
        elif payload.format.value in {"hwp", "hwpx", "docx", "pptx", "xlsx", "pdf"}:
            raise SyncPipelineError(
                "CONVERTER_UNAVAILABLE",
                "doc2md is required to synchronize binary document formats",
                retryable=True,
            )
        else:
            try:
                converted_body = source.content.decode("utf-8")
            except UnicodeDecodeError as error:
                raise SyncPipelineError(
                    "CONVERTER_UNAVAILABLE", "binary source requires doc2md"
                ) from error
            converted_sections = ()

        title, canonical_text, document_chunks = _canonical_and_chunks_v2(
            document_id=payload.document_id,
            revision=revision,
            file_version_id=file_version_id,
            body=converted_body,
            sections=converted_sections,
            fallback_title=str(manifest["title"]),
        )
        canonical_path = candidate / str(manifest["canonical_path"])
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        canonical_path.write_text(canonical_text, encoding="utf-8", newline="\n")
        chunks = [item for item in chunks if item["document_id"] != payload.document_id]
        chunks.extend(document_chunks)
        links = [
            item
            for item in links
            if item["source_id"] != payload.document_id and item["target_id"] != payload.document_id
        ]
        manifest.update(
            {
                "title": title,
                "revision": revision,
                "file_version_id": file_version_id,
                "status": "active",
                "source": {
                    "filename": PurePosixPath(payload.source_uri.split("source://", 1)[-1]).name,
                    "uri": payload.source_uri,
                    "media_type": _media_type_v2(payload.format.value),
                    "sha256": payload.after_sha256,
                },
                "conversion": {
                    "converter": converter_name,
                    "conversion_version": converter_version,
                    "converted_at": datetime.now(UTC).isoformat(),
                    "status": "succeeded",
                },
            }
        )
        reference = candidate / f"references/{payload.document_id}.source.json"
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_text(
            _json(
                {
                    "schema_version": "1.0.0",
                    "kind": "source_reference",
                    "document_id": payload.document_id,
                    "source_uri": payload.source_uri,
                    "source_sha256": payload.after_sha256,
                }
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        _write_jsonl(manifest_path, manifests)
        _write_jsonl(chunks_path, chunks)
        _write_jsonl(links_path, links)
        return True

    async def _build_candidate(
        self,
        candidate: Path,
        *,
        parent_version: str,
        candidate_version: str,
        events: list[PendingEvent],
    ) -> bool:
        payloads = [ContentChangedPayload.model_validate(event.payload) for event in events]
        event_ids = [event.event_id for event in events]
        if [payload.event_id for payload in payloads] != event_ids:
            raise SyncPipelineError(
                "EVENT_ID_MISMATCH",
                "outbox envelope과 payload event_id가 일치하지 않습니다.",
            )
        self._state.record_sync_task(event_ids, stage="convert", status="running")
        try:
            changed = await asyncio.to_thread(
                self._apply_converted_documents,
                candidate,
                payloads,
            )
        except BaseException:
            self._state.record_sync_task(event_ids, stage="convert", status="failed")
            raise
        self._state.record_sync_task(event_ids, stage="convert", status="succeeded")
        if not changed:
            return False

        stage_results = await asyncio.gather(
            self._run_stage(event_ids, "fts", self._build_fts, candidate),
            self._run_stage(event_ids, "vector", self._build_vectors, candidate),
            self._run_stage(event_ids, "graph", self._build_graph, candidate),
            return_exceptions=True,
        )
        for stage_result in stage_results:
            if isinstance(stage_result, BaseException):
                raise stage_result

        self._state.record_sync_task(event_ids, stage="validate", status="running")
        await asyncio.to_thread(
            self._finalize_candidate,
            candidate,
            parent_version,
            candidate_version,
            event_ids,
        )
        try:
            self._catalog.validate_candidate(candidate)
        except BaseException:
            self._state.record_sync_task(event_ids, stage="validate", status="failed")
            raise
        self._state.record_sync_task(event_ids, stage="validate", status="succeeded")
        return True

    async def _run_stage(
        self,
        event_ids: list[str],
        stage: str,
        function: Any,
        candidate: Path,
    ) -> None:
        self._state.record_sync_task(event_ids, stage=stage, status="running")
        try:
            await asyncio.to_thread(function, candidate)
        except BaseException:
            self._state.record_sync_task(event_ids, stage=stage, status="failed")
            raise
        self._state.record_sync_task(event_ids, stage=stage, status="succeeded")

    def _apply_converted_documents(
        self,
        candidate: Path,
        payloads: list[ContentChangedPayload],
    ) -> bool:
        manifest_path = candidate / "manifest.jsonl"
        chunks_path = candidate / "retrieval/chunks.jsonl"
        links_path = candidate / "retrieval/links.jsonl"
        manifests = _read_jsonl(manifest_path)
        chunks = _read_jsonl(chunks_path)
        links = _read_jsonl(links_path)
        manifest_by_id = {item["id"]: item for item in manifests}
        changed = False
        final_payload_by_document: dict[str, ContentChangedPayload] = {}

        for payload in payloads:
            manifest = manifest_by_id.get(payload.document_id)
            if manifest is None:
                raise ValueError(f"event references unknown document: {payload.document_id}")
            if manifest["source"]["uri"] != payload.source_uri:
                raise ValueError(f"event source URI mismatch: {payload.document_id}")
            if manifest["source"]["sha256"] == payload.after_sha256:
                continue
            if manifest["source"]["sha256"] != payload.before_sha256:
                raise ValueError(f"event base hash mismatch: {payload.document_id}")

            canonical_path = candidate / manifest["canonical_path"]
            canonical_text = canonical_path.read_text(encoding="utf-8")
            operation = payload.operation
            next_revision = _next_revision(str(manifest["revision"]))
            if canonical_text.count(operation.expected_text) != operation.expected_occurrences:
                raise ValueError(f"canonical anchor mismatch: {payload.document_id}")
            converted_text = canonical_text.replace(
                operation.expected_text,
                operation.replacement_text,
                1,
            )
            canonical_path.write_text(converted_text, encoding="utf-8")

            chunk_matches = 0
            chunk_id_updates: dict[str, str] = {}
            title = str(manifest["title"])
            if operation.expected_text in title:
                manifest["title"] = title.replace(
                    operation.expected_text,
                    operation.replacement_text,
                    1,
                )
            for chunk in chunks:
                if chunk["document_id"] != payload.document_id:
                    continue
                matched = False
                occurrences = str(chunk["text"]).count(operation.expected_text)
                if occurrences:
                    if occurrences != 1:
                        raise ValueError(f"chunk anchor is ambiguous: {chunk['chunk_id']}")
                    chunk["text"] = str(chunk["text"]).replace(
                        operation.expected_text,
                        operation.replacement_text,
                        1,
                    )
                    matched = True
                updated_headings = []
                for heading in chunk["heading_path"]:
                    heading_text = str(heading)
                    if operation.expected_text in heading_text:
                        heading_text = heading_text.replace(
                            operation.expected_text,
                            operation.replacement_text,
                            1,
                        )
                        matched = True
                    updated_headings.append(heading_text)
                chunk["heading_path"] = updated_headings
                chunk["section"] = updated_headings[-1]
                if matched:
                    chunk_matches += 1
                chunk["file_version_id"] = payload.file_version_id
                chunk["text_sha256"] = _sha256(str(chunk["text"]).encode("utf-8"))
                chunk["embedding_text"] = " ".join(
                    [
                        str(manifest["title"]),
                        *updated_headings,
                        str(chunk["text"]),
                    ]
                )
                old_chunk_id = str(chunk["chunk_id"])
                new_chunk_id = _chunk_id(
                    payload.document_id,
                    next_revision,
                    str(chunk["section_id"]),
                    int(chunk["ordinal"]),
                )
                chunk["chunk_id"] = new_chunk_id
                chunk_id_updates[old_chunk_id] = new_chunk_id
            if chunk_matches == 0:
                raise ValueError(
                    f"no retrieval chunk contains the changed anchor: {payload.document_id}"
                )
            for link in links:
                link["evidence_chunk_ids"] = [
                    chunk_id_updates.get(str(chunk_id), str(chunk_id))
                    for chunk_id in link["evidence_chunk_ids"]
                ]

            manifest["file_version_id"] = payload.file_version_id
            manifest["source"]["sha256"] = payload.after_sha256
            manifest["revision"] = next_revision
            changed = True
            final_payload_by_document[payload.document_id] = payload

        for document_id, payload in final_payload_by_document.items():
            manifest = manifest_by_id[document_id]
            source = self._files.snapshot(payload.source_uri)
            if source.sha256 != payload.after_sha256:
                raise ValueError(f"live source changed during conversion: {document_id}")
            converter_name = "local-markdown-adapter"
            conversion_version = "1.0.0"
            if self._doc2md is not None:
                converted = self._doc2md.convert(
                    ManifestEntry.model_validate(manifest),
                    expected_source_sha256=payload.after_sha256,
                )
                canonical_path = candidate / manifest["canonical_path"]
                canonical_path.write_text(
                    _restore_section_anchors(
                        converted.body,
                        document_id=document_id,
                        chunks=chunks,
                        converted_sections=converted.sections,
                    ),
                    encoding="utf-8",
                )
                converter_name = converted.converter
                conversion_version = converted.converter_version or "unknown"
            manifest["conversion"] = {
                "converter": converter_name,
                "conversion_version": conversion_version,
                "converted_at": datetime.now(UTC).isoformat(),
                "status": "succeeded",
            }
            reference_path = candidate / f"references/{payload.document_id}.source.json"
            reference_path.parent.mkdir(parents=True, exist_ok=True)
            reference_path.write_text(
                _json(
                    {
                        "schema_version": "1.0.0",
                        "kind": "source_reference",
                        "document_id": payload.document_id,
                        "source_uri": payload.source_uri,
                        "source_sha256": payload.after_sha256,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

        _write_jsonl(manifest_path, manifests)
        _write_jsonl(chunks_path, chunks)
        _write_jsonl(links_path, links)
        return changed

    def _build_fts(self, candidate: Path) -> None:
        self._raise_if_failed("fts")
        chunks = _read_jsonl(candidate / "retrieval/chunks.jsonl")
        rows = []
        for chunk in chunks:
            tokens = sorted(set(re.findall(r"[0-9A-Za-z가-힣]+", str(chunk["text"]).lower())))
            rows.append(
                {
                    "chunk_id": chunk["chunk_id"],
                    "document_id": chunk["document_id"],
                    "tokens": tokens,
                }
            )
        path = candidate / "indexes/fts/documents.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_jsonl(path, rows)

    def _build_vectors(self, candidate: Path) -> None:
        self._raise_if_failed("vector")
        chunks = _read_jsonl(candidate / "retrieval/chunks.jsonl")
        rows = []
        for chunk in chunks:
            rows.append(
                {
                    "chunk_id": chunk["chunk_id"],
                    "document_id": chunk["document_id"],
                    "embedding": _deterministic_embedding(str(chunk["embedding_text"])),
                    "adapter": "local-hash-vector-v1",
                }
            )
        path = candidate / "indexes/vector/embeddings.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_jsonl(path, rows)

    def _build_graph(self, candidate: Path) -> None:
        self._raise_if_failed("graph")
        links = _read_jsonl(candidate / "retrieval/links.jsonl")
        path = candidate / "indexes/graph/edges.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_jsonl(path, links)

    def _finalize_candidate(
        self,
        candidate: Path,
        parent_version: str,
        version: str,
        event_ids: list[str],
    ) -> None:
        (candidate / "VERSION").write_text(f"{version}\n", encoding="utf-8")
        metadata = {
            "schema_version": "1.0.0",
            "version": version,
            "parent_version": parent_version,
            "event_ids": event_ids,
            "converter": "local-markdown-adapter",
            "fts": "local-token-index-v1",
            "vector": "local-hash-vector-v1",
            "graph": "local-edge-index-v1",
        }
        metadata_path = candidate / "indexes/index-meta.json"
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(_json(metadata) + "\n", encoding="utf-8")
        _write_checksums(candidate)

    def _raise_if_failed(self, stage: str) -> None:
        if self._fail_stage == stage:
            raise ValueError(f"injected {stage} adapter failure")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    payload = "".join(f"{_json(row)}\n" for row in rows)
    path.write_text(payload, encoding="utf-8")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_checksums(root: Path) -> None:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        relative_parts = path.relative_to(root).parts
        if relative == "checksums.sha256" or any(part.startswith(".") for part in relative_parts):
            continue
        rows.append(f"{_sha256(path.read_bytes())}  {relative}\n")
    (root / "checksums.sha256").write_text("".join(rows), encoding="utf-8")


def _deterministic_embedding(text: str, dimensions: int = 32) -> list[float]:
    vector = [0.0] * dimensions
    tokens = re.findall(r"[0-9A-Za-z가-힣]+", text.lower())
    for token in tokens:
        digest = hashlib.sha256(token.encode()).digest()
        index = int.from_bytes(digest[:2], "big") % dimensions
        vector[index] += -1.0 if digest[2] & 1 else 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [round(value / norm, 6) for value in vector]


def _candidate_version(parent_version: str, event_ids: list[str]) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    digest = hashlib.sha256("\x00".join(event_ids).encode()).hexdigest()[:10]
    parent = re.sub(r"[^0-9A-Za-z._-]+", "-", parent_version)[:40]
    return f"{timestamp}-{digest}-from-{parent}"


def _next_revision(revision: str) -> str:
    try:
        return str(int(revision) + 1)
    except ValueError:
        return f"{revision}+1"


def _chunk_id(document_id: str, revision: str, section_id: str, ordinal: int) -> str:
    ordinal_suffix = f":{ordinal}" if ordinal else ""
    return f"{document_id}@{revision}#{section_id}{ordinal_suffix}"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _restore_section_anchors(
    body: str,
    *,
    document_id: str,
    chunks: list[dict[str, Any]],
    converted_sections: tuple[ConversionSection, ...],
) -> str:
    section_id_by_path: dict[tuple[str, ...], str] = {}
    section_path_by_id: dict[str, tuple[str, ...]] = {}
    for chunk in chunks:
        if chunk["document_id"] != document_id:
            continue
        heading_path = tuple(str(item) for item in chunk["heading_path"])
        section_id = str(chunk["section_id"])
        existing = section_id_by_path.setdefault(heading_path, section_id)
        if existing != section_id:
            raise ValueError(f"duplicate heading path has different section IDs: {document_id}")
        existing_path = section_path_by_id.setdefault(section_id, heading_path)
        if existing_path != heading_path:
            raise ValueError(f"duplicate section ID has different heading paths: {document_id}")

    converted_by_path: dict[tuple[str, ...], ConversionSection] = {}
    converted_evidence_sections: list[ConversionSection] = []
    for section in converted_sections:
        if len(section.heading_path) < 2:
            continue
        if section.heading_path in converted_by_path:
            raise ValueError(f"doc2md returned duplicate heading path: {document_id}")
        converted_by_path[section.heading_path] = section
        converted_evidence_sections.append(section)

    mapped_sections: list[tuple[str, ConversionSection]] = []
    unmatched_existing: list[tuple[tuple[str, ...], str]] = []
    used_converted_paths: set[tuple[str, ...]] = set()
    for heading_path, section_id in section_id_by_path.items():
        converted_section = converted_by_path.get(heading_path)
        if converted_section is None:
            unmatched_existing.append((heading_path, section_id))
            continue
        mapped_sections.append((section_id, converted_section))
        used_converted_paths.add(heading_path)

    unmatched_converted = [
        section
        for section in converted_evidence_sections
        if section.heading_path not in used_converted_paths
    ]
    if len(unmatched_existing) != len(unmatched_converted):
        raise ValueError(f"doc2md omitted an existing evidence section: {document_id}")
    mapped_sections.extend(
        (section_id, converted_section)
        for (_, section_id), converted_section in zip(
            unmatched_existing, unmatched_converted, strict=True
        )
    )

    rendered = body
    existing_lines = set(body.splitlines())
    insertions = sorted(
        (
            converted_section.char_start,
            f'<a id="{section_id}"></a>\n',
        )
        for section_id, converted_section in mapped_sections
        if f'<a id="{section_id}"></a>' not in existing_lines
    )
    for offset, anchor in reversed(insertions):
        rendered = rendered[:offset] + anchor + rendered[offset:]

    indexed = build_section_index(rendered)
    for section_id, converted_section in mapped_sections:
        indexed_section = indexed.get(section_id)
        if (
            indexed_section is None
            or indexed_section.heading_path != converted_section.heading_path
        ):
            raise ValueError(f"doc2md evidence section mapping is invalid: {document_id}")
    return rendered


def _new_manifest_v2(payload: DocumentContentChangedV2, revision: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "id": payload.document_id,
        "file_version_id": "dfv_" + (payload.after_sha256 or "0" * 64)[:24],
        "title": payload.document_id,
        "doc_type": "general",
        "language": "ko",
        "revision": revision,
        "status": "active",
        "official_number": None,
        "authority_level": "user-approved",
        "issuing_org": None,
        "issued_on": None,
        "effective_from": None,
        "effective_to": None,
        "source": {
            "filename": PurePosixPath(payload.source_uri.split("source://", 1)[-1]).name,
            "uri": payload.source_uri,
            "media_type": _media_type_v2(payload.format.value),
            "sha256": payload.after_sha256,
        },
        "canonical_path": f"docs/general/{payload.document_id}.md",
        "access": "internal",
        "write_access": "allowed",
        "editability": "editable",
        "tags": [],
        "aliases": [],
        "conversion": {
            "converter": "pending",
            "conversion_version": "pending",
            "converted_at": None,
            "status": "pending",
        },
    }


def _canonical_and_chunks_v2(
    *,
    document_id: str,
    revision: str,
    file_version_id: str,
    body: str,
    sections: tuple[ConversionSection, ...],
    fallback_title: str,
) -> tuple[str, str, list[dict[str, Any]]]:
    normalized = body.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
    title_match = re.search(r"^#\s+(.+)$", normalized, flags=re.MULTILINE)
    title = title_match.group(1).strip() if title_match else fallback_title
    chunks: list[dict[str, Any]] = []
    if sections:
        insertions: list[tuple[int, str]] = []
        for index, section in enumerate(sections):
            section_id = section.stable_key
            insertions.append((section.char_start, f'<a id="{section_id}"></a>\n'))
            text = normalized[section.char_start : min(section.char_end, len(normalized))].strip()
            heading_path = list(section.heading_path) or [title]
            if not text:
                text = heading_path[-1]
            chunks.append(
                _chunk_v2(
                    document_id=document_id,
                    revision=revision,
                    file_version_id=file_version_id,
                    section_id=section_id,
                    heading_path=heading_path,
                    text=text,
                    ordinal=index,
                    title=title,
                )
            )
        canonical = normalized
        for offset, anchor in sorted(insertions, reverse=True):
            canonical = canonical[:offset] + anchor + canonical[offset:]
    else:
        section_id = "sec-001"
        heading_path = [title]
        canonical = normalized
        if title_match:
            line_end = canonical.find("\n", title_match.end()) + 1
            canonical = canonical[:line_end] + f'<a id="{section_id}"></a>\n' + canonical[line_end:]
        else:
            canonical = f'# {title}\n<a id="{section_id}"></a>\n\n{canonical}'
        chunks.append(
            _chunk_v2(
                document_id=document_id,
                revision=revision,
                file_version_id=file_version_id,
                section_id=section_id,
                heading_path=heading_path,
                text=normalized.strip() or title,
                ordinal=0,
                title=title,
            )
        )
    return title, canonical, chunks


def _chunk_v2(
    *,
    document_id: str,
    revision: str,
    file_version_id: str,
    section_id: str,
    heading_path: list[str],
    text: str,
    ordinal: int,
    title: str,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "chunk_id": _chunk_id(document_id, revision, section_id, ordinal),
        "document_id": document_id,
        "file_version_id": file_version_id,
        "section_id": section_id,
        "section": heading_path[-1],
        "heading_path": heading_path,
        "text": text,
        "embedding_text": " ".join([title, *heading_path, text]),
        "text_sha256": _sha256(text.encode("utf-8")),
        "ordinal": ordinal,
    }


def _media_type_v2(format_: str) -> str:
    return {
        "hwp": "application/x-hwp",
        "hwpx": "application/vnd.hancom.hwpx",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "pdf": "application/pdf",
    }[format_]
