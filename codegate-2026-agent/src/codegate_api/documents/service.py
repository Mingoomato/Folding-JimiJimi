from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Callable, Coroutine, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from codegate_filesystem import fsync_directory, fsync_file

from codegate_api.documents.capabilities import (
    CapabilityUnavailable,
    DocumentCapabilityRegistry,
)
from codegate_api.documents.models import (
    ApproveDocumentPlanRequest,
    ArtifactView,
    CapabilityRegistryView,
    DocumentCreationPlanRequest,
    DocumentExecutionView,
    DocumentFormat,
    DocumentPlanView,
    HwpDerivationPayload,
    MarkdownCreationPayload,
    MutationPlanRequest,
    PlanKind,
    PreviewManifest,
    PreviewPair,
    RejectDocumentPlanRequest,
    RejectDocumentPlanResponse,
    SourceStructureView,
    SpreadsheetCellsSetOperation,
    StructuralDiff,
    TableCellSetOperation,
    TextReplaceOperation,
)
from codegate_api.documents.rendering import ManagedDocumentRenderer, RenderedPage
from codegate_api.documents.store import (
    DocumentStateError,
    DocumentStateStore,
    StoredDocumentExecution,
    StoredDocumentPlan,
)
from codegate_api.documents.templates import (
    ApprovedTemplate,
    DocumentTemplateRegistry,
    TemplateRegistryError,
)
from codegate_api.documents.writers.base import ProposedDocument, WriterError
from codegate_api.documents.writers.registry import WriterRegistry
from codegate_api.files.atomic import (
    DocumentLockManager,
    FileHashConflict,
    SafeSourceFileStore,
)
from codegate_api.files.resolver import SourceUriResolver
from codegate_api.knowledge.access import AccessContext
from codegate_api.knowledge.catalog import KnowledgeCatalog
from codegate_api.knowledge.llmwiki import NativeLLMWikiCatalog


class DocumentServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


class DocumentService:
    def __init__(
        self,
        *,
        plan_ttl_seconds: int,
        retention_days: int,
        artifact_root: Path,
        max_preview_bytes: int,
        resolver: SourceUriResolver,
        files: SafeSourceFileStore,
        locks: DocumentLockManager,
        catalog: KnowledgeCatalog | NativeLLMWikiCatalog,
        state: DocumentStateStore,
        capabilities: DocumentCapabilityRegistry,
        writers: WriterRegistry,
        renderer: ManagedDocumentRenderer,
        templates: DocumentTemplateRegistry,
        sync_notifier: Callable[[], None],
    ) -> None:
        self._plan_ttl = timedelta(seconds=plan_ttl_seconds)
        self._retention = timedelta(days=retention_days)
        self._artifact_root = artifact_root.resolve()
        self._max_preview_bytes = max_preview_bytes
        self._resolver = resolver
        self._files = files
        self._locks = locks
        self._catalog = catalog
        self._state = state
        self._capabilities = capabilities
        self._writers = writers
        self._renderer = renderer
        self._templates = templates
        self._sync_notifier = sync_notifier
        self._tasks: set[asyncio.Task[None]] = set()

    async def shutdown(self) -> None:
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        self._writers.close()

    async def recover_prepared_executions(self) -> None:
        for stored in self._state.prepared_executions():
            try:
                if stored.values["undo_of_execution_id"]:
                    await self._recover_prepared_undo(stored.values)
                else:
                    await self._recover_prepared_apply(stored.values)
            except DocumentServiceError as error:
                self._state.mark_execution_error(
                    str(stored.values["id"]),
                    status="conflict" if error.status_code in {409, 423} else "failed",
                    code=error.code,
                    message=str(error),
                    retryable=error.retryable,
                )
            except Exception as error:
                self._state.mark_execution_error(
                    str(stored.values["id"]),
                    status="failed",
                    code="recovery_failed",
                    message=str(error),
                    retryable=False,
                )

    async def prune_expired_managed_files(self) -> list[Path]:
        cutoff = datetime.now(UTC) - self._retention
        return await asyncio.to_thread(
            self._files.prune_expired_managed_files,
            older_than=cutoff,
        )

    def capabilities(self) -> CapabilityRegistryView:
        return self._capabilities.snapshot()

    async def source_structure(
        self,
        document_id: str,
        *,
        access_context: AccessContext,
    ) -> SourceStructureView:
        manifest = self._manifest(document_id, access_context, write=False)
        format_ = _format_from_uri(manifest.source.uri)
        snapshot = await asyncio.to_thread(self._files.snapshot, manifest.source.uri)
        registry = self._capabilities.snapshot()
        items = await asyncio.to_thread(self._writers.read_structure, snapshot.content, format_)
        return SourceStructureView(
            document_id=document_id,
            format=format_,
            source_sha256=snapshot.sha256,
            capability_snapshot_id=registry.snapshot_id,
            graph_version=self._catalog.snapshot().version,
            items=items,
        )

    def queue_mutation(
        self,
        document_id: str,
        request: MutationPlanRequest,
        *,
        access_context: AccessContext,
        idempotency_key: str,
    ) -> DocumentPlanView:
        tenant_id, subject_id = _identity(access_context)
        manifest = self._manifest(document_id, access_context, write=True)
        format_ = _format_from_uri(manifest.source.uri)
        operation_types = [operation.type for operation in request.operations]
        self._require_capability(
            capability_id=request.capability_id,
            format_=format_,
            operation_types=operation_types,
            snapshot_id=request.capability_snapshot_id,
        )
        graph_version = self._catalog.snapshot().version
        if graph_version != request.graph_version:
            raise DocumentServiceError(
                "graph_version_conflict",
                "LLMWIKI graph version changed",
                status_code=409,
            )
        plan_id = "dplan_" + uuid.uuid4().hex
        now = datetime.now(UTC)
        request_json = request.model_dump(mode="json")
        request_hash = _hash_json({"document_id": document_id, **request_json})
        try:
            stored_id = self._state.create_preparing_plan(
                plan_id=plan_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                kind=PlanKind.MUTATION,
                document_id=document_id,
                format_=format_,
                capability_id=request.capability_id,
                source_uri=manifest.source.uri,
                target_relative_path=None,
                request=request_json,
                operations=[item.model_dump(mode="json") for item in request.operations],
                graph_version=request.graph_version,
                capability_snapshot_id=request.capability_snapshot_id,
                created_at=now,
                expires_at=now + self._plan_ttl,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
        except DocumentStateError as error:
            raise _state_error(error) from error
        if stored_id == plan_id:
            self._spawn(
                self._prepare_mutation(plan_id, document_id, format_, request, access_context),
                name=f"prepare-document-plan:{plan_id}",
            )
        return self.get_plan(stored_id, access_context=access_context)

    def queue_creation(
        self,
        request: DocumentCreationPlanRequest,
        *,
        access_context: AccessContext,
        idempotency_key: str,
    ) -> DocumentPlanView:
        tenant_id, subject_id = _identity(access_context)
        _require_creation_access(request.document_id, access_context)
        template: ApprovedTemplate | None = None
        if isinstance(request.payload, MarkdownCreationPayload) and request.payload.template_id:
            template_format = (
                DocumentFormat.DOCX if request.format is DocumentFormat.PDF else request.format
            )
            if template_format not in {DocumentFormat.DOCX, DocumentFormat.PPTX}:
                raise DocumentServiceError(
                    "unsupported_template",
                    f"approved templates are not supported for {request.format.value}",
                    status_code=422,
                )
            try:
                template = self._templates.get(
                    request.payload.template_id,
                    format_=template_format,
                )
            except TemplateRegistryError as error:
                raise DocumentServiceError(
                    error.code,
                    str(error),
                    status_code=422,
                ) from error
        target = PurePosixPath(request.target_relative_path.replace("\\", "/"))
        source_uri = "source://" + target.as_posix()
        target_path = self._resolver.resolve(source_uri)
        if target_path.exists():
            raise DocumentServiceError(
                "target_exists", "creation target already exists", status_code=409
            )
        operation_type = request.payload.type
        capability_format = (
            DocumentFormat.HWP
            if isinstance(request.payload, HwpDerivationPayload)
            else request.format
        )
        self._require_capability(
            capability_id=request.capability_id,
            format_=capability_format,
            operation_types=[operation_type],
            snapshot_id=request.capability_snapshot_id,
        )
        if request.format is DocumentFormat.PDF and not self._renderer.libreoffice_available:
            raise DocumentServiceError(
                "renderer_unavailable",
                "PDF Markdown creation requires managed LibreOffice",
                status_code=503,
                retryable=True,
            )
        graph_version = self._catalog.snapshot().version
        if graph_version != request.graph_version:
            raise DocumentServiceError(
                "graph_version_conflict",
                "LLMWIKI graph version changed",
                status_code=409,
            )
        plan_id = "dplan_" + uuid.uuid4().hex
        now = datetime.now(UTC)
        request_json = request.model_dump(mode="json")
        request_hash = _hash_json(request_json)
        kind = (
            PlanKind.DERIVATION
            if isinstance(request.payload, HwpDerivationPayload)
            else PlanKind.CREATION
        )
        try:
            stored_id = self._state.create_preparing_plan(
                plan_id=plan_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                kind=kind,
                document_id=request.document_id,
                format_=request.format,
                capability_id=request.capability_id,
                source_uri=source_uri,
                target_relative_path=target.as_posix(),
                request=request_json,
                operations=[request.payload.model_dump(mode="json")],
                graph_version=request.graph_version,
                capability_snapshot_id=request.capability_snapshot_id,
                created_at=now,
                expires_at=now + self._plan_ttl,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
        except DocumentStateError as error:
            raise _state_error(error) from error
        if stored_id == plan_id:
            self._spawn(
                self._prepare_creation(plan_id, request, access_context, template),
                name=f"prepare-document-plan:{plan_id}",
            )
        return self.get_plan(stored_id, access_context=access_context)

    def get_plan(
        self,
        plan_id: str,
        *,
        access_context: AccessContext,
    ) -> DocumentPlanView:
        try:
            stored = self._state.get_plan(plan_id)
        except DocumentStateError as error:
            raise _state_error(error) from error
        _require_owner(stored.values, access_context, "plan_not_found")
        if stored.values["status"] == "expired":
            self._cleanup_plan_artifacts(plan_id)
        return _plan_view(stored)

    def preview_path(
        self,
        plan_id: str,
        artifact_id: str,
        *,
        access_context: AccessContext,
    ) -> tuple[Path, str]:
        try:
            plan = self._state.get_plan(plan_id)
        except DocumentStateError as error:
            raise _state_error(error) from error
        _require_owner(plan.values, access_context, "plan_not_found")
        artifact = next(
            (
                item
                for item in plan.artifacts
                if item["id"] == artifact_id and item["kind"] in {"preview_before", "preview_after"}
            ),
            None,
        )
        if artifact is None:
            raise DocumentServiceError(
                "artifact_not_found", "preview artifact not found", status_code=404
            )
        path = self._verified_artifact(artifact)
        return path, str(artifact["mime_type"])

    async def approve(
        self,
        plan_id: str,
        body: ApproveDocumentPlanRequest,
        *,
        access_context: AccessContext,
        idempotency_key: str,
    ) -> DocumentExecutionView:
        tenant_id, subject_id = _identity(access_context)
        request_hash = _hash_json({"plan_id": plan_id, "plan_hash": body.plan_hash})
        try:
            existing = self._state.idempotent_resource(
                scope="document-plan-approve",
                tenant_id=tenant_id,
                subject_id=subject_id,
                key=idempotency_key,
                request_hash=request_hash,
            )
        except DocumentStateError as error:
            raise _state_error(error) from error
        if existing is not None:
            return self.get_execution(existing, access_context=access_context)
        plan = self._owned_plan(plan_id, access_context)
        if plan.values["plan_hash"] != body.plan_hash:
            raise DocumentServiceError(
                "plan_hash_conflict", "plan hash does not match", status_code=409
            )
        self._authorize_plan(plan, access_context)
        proposed = self._proposed_artifact(plan)
        artifact_path = self._verified_artifact(proposed)
        content = artifact_path.read_bytes()
        if hashlib.sha256(content).hexdigest() != plan.values["proposed_sha256"]:
            raise DocumentServiceError(
                "artifact_hash_conflict", "proposed artifact changed", status_code=409
            )
        format_ = DocumentFormat(plan.values["format"])
        capability_format = (
            DocumentFormat.HWP if plan.values["kind"] == PlanKind.DERIVATION else format_
        )
        self._require_capability(
            capability_id=plan.values["capability_id"],
            format_=capability_format,
            operation_types=[str(item["type"]) for item in plan.values["operations"]],
            snapshot_id=plan.values["capability_snapshot_id"],
        )
        if plan.values["base_sha256"] and plan.values["kind"] == PlanKind.MUTATION:
            snapshot = await asyncio.to_thread(self._files.snapshot, plan.values["source_uri"])
            if snapshot.sha256 != plan.values["base_sha256"]:
                raise DocumentServiceError(
                    "stale_source", "source changed after preview", status_code=409
                )
        elif plan.values["kind"] == PlanKind.DERIVATION:
            payload = HwpDerivationPayload.model_validate(plan.values["operations"][0])
            source_manifest = self._manifest(
                payload.source_document_id,
                access_context,
                write=False,
            )
            source = await asyncio.to_thread(self._files.snapshot, source_manifest.source.uri)
            if source.sha256 != plan.values["base_sha256"]:
                raise DocumentServiceError("stale_source", "HWP source changed", status_code=409)
        execution_id = "exec_" + uuid.uuid4().hex
        change_kind = {
            PlanKind.MUTATION: "update",
            PlanKind.CREATION: "create",
            PlanKind.DERIVATION: "derive",
        }[PlanKind(plan.values["kind"])]
        try:
            stored_id = self._state.approve_plan(
                plan_id=plan_id,
                supplied_plan_hash=body.plan_hash,
                tenant_id=tenant_id,
                subject_id=subject_id,
                actor_id=subject_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                execution={
                    "id": execution_id,
                    "approval_id": "approval_" + uuid.uuid4().hex,
                    "document_id": plan.values["document_id"],
                    "change_kind": change_kind,
                    "format": format_.value,
                    "capability_id": plan.values["capability_id"],
                    "source_uri": plan.values["source_uri"],
                    "before_sha256": plan.values["base_sha256"],
                    "after_sha256": plan.values["proposed_sha256"],
                    "artifact_sha256": proposed["sha256"],
                    "graph_version_before": self._catalog.snapshot().version,
                },
            )
        except DocumentStateError as error:
            raise _state_error(error) from error
        if stored_id != execution_id:
            return self.get_execution(stored_id, access_context=access_context)
        try:
            await self._apply_execution(execution_id, plan, content, access_context)
        except DocumentServiceError as error:
            status = "conflict" if error.status_code in {409, 423} else "failed"
            self._state.mark_execution_error(
                execution_id,
                status=status,
                code=error.code,
                message=str(error),
                retryable=error.retryable,
            )
        return self.get_execution(execution_id, access_context=access_context)

    def reject(
        self,
        plan_id: str,
        body: RejectDocumentPlanRequest,
        *,
        access_context: AccessContext,
        idempotency_key: str,
    ) -> RejectDocumentPlanResponse:
        tenant_id, subject_id = _identity(access_context)
        plan = self._owned_plan(plan_id, access_context)
        self._authorize_plan(plan, access_context)
        try:
            self._state.reject_plan(
                plan_id=plan_id,
                supplied_plan_hash=body.plan_hash,
                reason=body.reason,
                tenant_id=tenant_id,
                subject_id=subject_id,
                actor_id=subject_id,
                idempotency_key=idempotency_key,
                request_hash=_hash_json(
                    {"plan_id": plan_id, "plan_hash": body.plan_hash, "reason": body.reason}
                ),
                approval_id="approval_" + uuid.uuid4().hex,
            )
        except DocumentStateError as error:
            raise _state_error(error) from error
        self._cleanup_plan_artifacts(plan_id)
        return RejectDocumentPlanResponse(change_plan_id=plan_id)

    def get_execution(
        self,
        execution_id: str,
        *,
        access_context: AccessContext,
    ) -> DocumentExecutionView:
        try:
            execution = self._state.get_execution(execution_id)
        except DocumentStateError as error:
            raise _state_error(error) from error
        _require_owner(execution.values, access_context, "execution_not_found")
        return _execution_view(execution)

    def retry_sync(
        self,
        execution_id: str,
        *,
        access_context: AccessContext,
        idempotency_key: str,
    ) -> DocumentExecutionView:
        tenant_id, subject_id = _identity(access_context)
        try:
            self._state.retry_event(
                execution_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                idempotency_key=idempotency_key,
                request_hash=_hash_json({"execution_id": execution_id}),
            )
        except DocumentStateError as error:
            raise _state_error(error) from error
        self._sync_notifier()
        return self.get_execution(execution_id, access_context=access_context)

    async def undo(
        self,
        execution_id: str,
        *,
        access_context: AccessContext,
        idempotency_key: str,
    ) -> DocumentExecutionView:
        tenant_id, subject_id = _identity(access_context)
        original = self._state.get_execution(execution_id).values
        _require_owner(original, access_context, "execution_not_found")
        undo_id = "exec_" + uuid.uuid4().hex
        try:
            stored_id = self._state.create_undo(
                original=original,
                execution_id=undo_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                idempotency_key=idempotency_key,
                request_hash=_hash_json({"execution_id": execution_id}),
                graph_version_before=self._catalog.snapshot().version,
            )
        except DocumentStateError as error:
            raise _state_error(error) from error
        if stored_id != undo_id:
            return self.get_execution(stored_id, access_context=access_context)
        try:
            await self._apply_undo(undo_id, original)
        except DocumentServiceError as error:
            self._state.mark_execution_error(
                undo_id,
                status="conflict" if error.status_code in {409, 423} else "failed",
                code=error.code,
                message=str(error),
                retryable=error.retryable,
            )
        return self.get_execution(undo_id, access_context=access_context)

    async def _prepare_mutation(
        self,
        plan_id: str,
        document_id: str,
        format_: DocumentFormat,
        request: MutationPlanRequest,
        access_context: AccessContext,
    ) -> None:
        try:
            manifest = self._manifest(document_id, access_context, write=True)
            snapshot = await asyncio.to_thread(self._files.snapshot, manifest.source.uri)
            if snapshot.sha256 != request.expected_source_sha256:
                raise DocumentServiceError(
                    "stale_source", "source SHA does not match", status_code=409
                )
            structure = await asyncio.to_thread(
                self._writers.read_structure,
                snapshot.content,
                format_,
            )
            _validate_operation_provenance(request.operations, structure)
            proposed = await asyncio.to_thread(
                self._writers.mutate,
                source=snapshot.content,
                format_=format_,
                operations=request.operations,
            )
            await asyncio.to_thread(
                self._finish_plan,
                plan_id,
                format_,
                snapshot.content,
                snapshot.sha256,
                proposed,
                request.model_dump(mode="json"),
                request.graph_version,
                request.capability_snapshot_id,
            )
        except Exception as error:
            self._fail_preparation(plan_id, error)

    async def _prepare_creation(
        self,
        plan_id: str,
        request: DocumentCreationPlanRequest,
        access_context: AccessContext,
        template: ApprovedTemplate | None,
    ) -> None:
        try:
            base_content: bytes | None = None
            base_sha: str | None = None
            if isinstance(request.payload, HwpDerivationPayload):
                manifest = self._manifest(
                    request.payload.source_document_id,
                    access_context,
                    write=False,
                )
                if _format_from_uri(manifest.source.uri) is not DocumentFormat.HWP:
                    raise DocumentServiceError(
                        "unsupported_feature",
                        "derivation source is not HWP",
                        status_code=422,
                    )
                snapshot = await asyncio.to_thread(self._files.snapshot, manifest.source.uri)
                if snapshot.sha256 != request.payload.expected_source_sha256:
                    raise DocumentServiceError(
                        "stale_source", "HWP source changed", status_code=409
                    )
                base_content = snapshot.content
                base_sha = snapshot.sha256
                proposed = await asyncio.to_thread(
                    self._writers.derive_hwp,
                    source=snapshot.content,
                    payload=request.payload,
                )
            else:
                proposed = await asyncio.to_thread(
                    self._writers.create,
                    format_=request.format,
                    payload=request.payload,
                    job_root=self._plan_root(plan_id),
                    template=template,
                )
            await asyncio.to_thread(
                self._finish_plan,
                plan_id,
                request.format,
                base_content,
                base_sha,
                proposed,
                request.model_dump(mode="json"),
                request.graph_version,
                request.capability_snapshot_id,
            )
        except Exception as error:
            self._fail_preparation(plan_id, error)

    def _finish_plan(
        self,
        plan_id: str,
        format_: DocumentFormat,
        before: bytes | None,
        base_sha256: str | None,
        proposed: ProposedDocument,
        request: dict[str, Any],
        graph_version: str,
        capability_snapshot_id: str,
    ) -> None:
        plan_root = self._plan_root(plan_id)
        suffix = "." + format_.value
        proposed_artifact = self._write_artifact(
            plan_root / ("proposed" + suffix),
            proposed.content,
            kind="proposed",
            mime_type=_mime_type(format_),
        )
        before_pages: list[RenderedPage] = []
        after_pages: list[RenderedPage] = []
        truncated = 0
        if format_ is DocumentFormat.HWPX:
            writer = self._writers.hwpx_writer()
            if writer is None:
                raise WriterError(
                    "renderer_unavailable", "Kordoc renderer is unavailable", retryable=True
                )
            if before is not None:
                before_pages = [RenderedPage(label, png) for label, png in writer.render(before)]
            after_pages = [
                RenderedPage(label, png) for label, png in writer.render(proposed.content)
            ]
            renderer_fingerprint = writer.fingerprint
        else:
            selected = _selected_pages(proposed.structural_diff, format_)
            if before is not None:
                before_pages, before_truncated = self._renderer.render(
                    content=before,
                    format_=format_,
                    job_root=plan_root / "render-before",
                    selected_pages=selected,
                )
                truncated = max(truncated, before_truncated)
            after_pages, after_truncated = self._renderer.render(
                content=proposed.content,
                format_=format_,
                job_root=plan_root / "render-after",
                selected_pages=selected,
            )
            truncated = max(truncated, after_truncated)
            renderer_fingerprint = self._renderer.fingerprint
        artifacts = [proposed_artifact]
        before_by_label: dict[str, str] = {}
        after_by_label: dict[str, str] = {}
        preview_total = 0
        for page in before_pages:
            preview_total += len(page.png)
            if preview_total > self._max_preview_bytes:
                truncated += 1
                continue
            artifact = self._write_artifact(
                plan_root / f"before-{page.label}.png",
                page.png,
                kind="preview_before",
                mime_type="image/png",
                preview_label=page.label,
            )
            artifacts.append(artifact)
            before_by_label[page.label] = artifact["id"]
        for page in after_pages:
            preview_total += len(page.png)
            if preview_total > self._max_preview_bytes:
                truncated += 1
                continue
            artifact = self._write_artifact(
                plan_root / f"after-{page.label}.png",
                page.png,
                kind="preview_after",
                mime_type="image/png",
                preview_label=page.label,
            )
            artifacts.append(artifact)
            after_by_label[page.label] = artifact["id"]
        labels = sorted(set(before_by_label) | set(after_by_label))
        pairs = [
            PreviewPair(
                locator_label=label,
                before_artifact_id=before_by_label.get(label),
                after_artifact_id=after_by_label.get(label),
            )
            for label in labels
        ]
        manifest_payload = {
            "pairs": [pair.model_dump(mode="json") for pair in pairs],
            "truncated_count": truncated,
            "artifacts": [
                {"id": item["id"], "sha256": item["sha256"], "kind": item["kind"]}
                for item in artifacts
                if item["kind"].startswith("preview_")
            ],
        }
        manifest_sha = _hash_json(manifest_payload)
        preview_manifest = PreviewManifest(
            manifest_sha256=manifest_sha,
            pairs=pairs,
            truncated_count=truncated,
        )
        stored = self._state.get_plan(plan_id)
        plan_hash = _hash_json(
            {
                "schema_version": "2.0.0",
                "kind": stored.values["kind"],
                "document_id": stored.values["document_id"],
                "format": format_.value,
                "capability_id": stored.values["capability_id"],
                "request": request,
                "operations": stored.values["operations"],
                "base_sha256": base_sha256,
                "proposed_sha256": proposed_artifact["sha256"],
                "preview_manifest_sha256": manifest_sha,
                "graph_version": graph_version,
                "capability_snapshot_id": capability_snapshot_id,
                "writer_fingerprint": proposed.writer_fingerprint,
                "renderer_fingerprint": renderer_fingerprint,
            }
        )
        self._state.complete_plan(
            plan_id=plan_id,
            base_sha256=base_sha256,
            proposed_sha256=proposed_artifact["sha256"],
            plan_hash=plan_hash,
            writer_fingerprint=proposed.writer_fingerprint,
            renderer_fingerprint=renderer_fingerprint,
            structural_diff=[item.model_dump(mode="json") for item in proposed.structural_diff],
            preview_manifest=preview_manifest.model_dump(mode="json"),
            warnings=proposed.warnings,
            artifacts=artifacts,
        )

    async def _apply_execution(
        self,
        execution_id: str,
        plan: StoredDocumentPlan,
        content: bytes,
        access_context: AccessContext,
    ) -> None:
        values = plan.values
        document_id = str(values["document_id"])
        source_uri = str(values["source_uri"])
        backup_path: str | None = None
        async with self._locks.acquire(document_id):
            self._authorize_plan(plan, access_context)
            proposed_sha = hashlib.sha256(content).hexdigest()
            if proposed_sha != values["proposed_sha256"]:
                raise DocumentServiceError(
                    "artifact_hash_conflict", "artifact changed", status_code=409
                )
            if values["kind"] == PlanKind.MUTATION:
                snapshot = await asyncio.to_thread(self._files.snapshot, source_uri)
                if snapshot.sha256 != values["base_sha256"]:
                    raise DocumentServiceError("stale_source", "source changed", status_code=409)
                backup = await asyncio.to_thread(self._files.write_backup, execution_id, snapshot)
                backup_path = str(backup)
                try:
                    written = await asyncio.to_thread(
                        self._files.atomic_replace,
                        source_uri=source_uri,
                        expected_sha256=snapshot.sha256,
                        new_content=content,
                    )
                except FileHashConflict as error:
                    raise DocumentServiceError(
                        "stale_source", "source changed", status_code=409
                    ) from error
            else:
                target = self._resolver.resolve(source_uri)
                if target.exists():
                    raise DocumentServiceError(
                        "target_exists", "creation target exists", status_code=409
                    )
                written = await asyncio.to_thread(
                    self._files.create_new,
                    source_uri=source_uri,
                    content=content,
                )
            if written.sha256 != proposed_sha:
                raise DocumentServiceError(
                    "write_verification_failed", "written file hash differs", status_code=503
                )
        event_id = "devent_" + uuid.uuid4().hex
        execution = self._state.get_execution(execution_id).values
        payload = {
            "schema_version": "2.0.0",
            "event_id": event_id,
            "execution_id": execution_id,
            "document_id": document_id,
            "source_uri": source_uri,
            "change_kind": execution["change_kind"],
            "format": execution["format"],
            "capability_id": execution["capability_id"],
            "before_sha256": execution["before_sha256"],
            "after_sha256": written.sha256,
            "artifact_sha256": execution["artifact_sha256"],
            "expected_source_sha256": written.sha256,
        }
        self._state.mark_file_applied(
            execution_id=execution_id,
            backup_path=backup_path,
            recovery_path=None,
            event_id=event_id,
            event_payload=payload,
        )
        self._sync_notifier()

    async def _recover_prepared_apply(self, execution: dict[str, Any]) -> None:
        plan_id = execution.get("plan_id")
        if not isinstance(plan_id, str):
            raise DocumentServiceError(
                "recovery_plan_missing", "prepared execution has no plan", status_code=409
            )
        try:
            plan = self._state.get_plan(plan_id)
        except DocumentStateError as error:
            raise DocumentServiceError(
                "recovery_plan_missing", "prepared execution plan is missing", status_code=409
            ) from error
        proposed = self._proposed_artifact(plan)
        content = self._verified_artifact(proposed).read_bytes()
        execution_id = str(execution["id"])
        source_uri = str(execution["source_uri"])
        document_id = str(execution["document_id"])
        backup_path: str | None = None
        async with self._locks.acquire(document_id):
            if execution["change_kind"] == "update":
                current = await asyncio.to_thread(self._files.snapshot, source_uri)
                backup = await asyncio.to_thread(
                    self._files.existing_backup_path,
                    execution_id,
                    source_uri,
                )
                if current.sha256 == execution["after_sha256"]:
                    if backup is None:
                        raise DocumentServiceError(
                            "recovery_backup_missing",
                            "applied mutation has no immutable backup",
                            status_code=409,
                        )
                    written = current
                elif current.sha256 == execution["before_sha256"]:
                    if backup is None:
                        backup = await asyncio.to_thread(
                            self._files.write_backup,
                            execution_id,
                            current,
                        )
                    written = await asyncio.to_thread(
                        self._files.atomic_replace,
                        source_uri=source_uri,
                        expected_sha256=current.sha256,
                        new_content=content,
                    )
                else:
                    raise DocumentServiceError(
                        "stale_source", "source changed during crash recovery", status_code=409
                    )
                backup_path = str(backup)
            else:
                if await asyncio.to_thread(self._files.is_absent, source_uri):
                    written = await asyncio.to_thread(
                        self._files.create_new,
                        source_uri=source_uri,
                        content=content,
                    )
                else:
                    written = await asyncio.to_thread(self._files.snapshot, source_uri)
                    if written.sha256 != execution["after_sha256"]:
                        raise DocumentServiceError(
                            "target_exists",
                            "creation target changed during recovery",
                            status_code=409,
                        )
        self._record_recovered_file_applied(
            execution,
            after_sha256=written.sha256,
            backup_path=backup_path,
            recovery_path=None,
        )

    async def _recover_prepared_undo(self, execution: dict[str, Any]) -> None:
        original = self._state.get_execution(str(execution["undo_of_execution_id"])).values
        execution_id = str(execution["id"])
        source_uri = str(execution["source_uri"])
        document_id = str(execution["document_id"])
        backup_path: str | None = None
        recovery_path: str | None = None
        after_sha256: str | None
        async with self._locks.acquire(document_id):
            if execution["change_kind"] == "recovery_remove":
                if await asyncio.to_thread(self._files.is_absent, source_uri):
                    recovery = await asyncio.to_thread(
                        self._files.existing_recovery_path,
                        execution_id,
                        source_uri,
                    )
                    if recovery is None:
                        raise DocumentServiceError(
                            "recovery_artifact_missing",
                            "removed creation has no recovery artifact",
                            status_code=409,
                        )
                else:
                    current = await asyncio.to_thread(self._files.snapshot, source_uri)
                    if current.sha256 != execution["before_sha256"]:
                        raise DocumentServiceError(
                            "stale_source",
                            "created source changed during recovery",
                            status_code=409,
                        )
                    recovery = await asyncio.to_thread(
                        self._files.move_to_recovery,
                        source_uri=source_uri,
                        execution_id=execution_id,
                    )
                recovery_path = str(recovery)
                after_sha256 = None
            else:
                if not original.get("backup_path"):
                    raise DocumentServiceError(
                        "backup_not_found", "original immutable backup is missing", status_code=409
                    )
                current = await asyncio.to_thread(self._files.snapshot, source_uri)
                backup = await asyncio.to_thread(
                    self._files.existing_backup_path,
                    execution_id,
                    source_uri,
                )
                if current.sha256 == execution["after_sha256"]:
                    if backup is None:
                        raise DocumentServiceError(
                            "recovery_backup_missing",
                            "applied Undo has no immutable redo backup",
                            status_code=409,
                        )
                    written = current
                elif current.sha256 == execution["before_sha256"]:
                    if backup is None:
                        backup = await asyncio.to_thread(
                            self._files.write_backup,
                            execution_id,
                            current,
                        )
                    prior_content = await asyncio.to_thread(
                        self._files.read_backup,
                        str(original["backup_path"]),
                    )
                    written = await asyncio.to_thread(
                        self._files.atomic_replace,
                        source_uri=source_uri,
                        expected_sha256=current.sha256,
                        new_content=prior_content,
                    )
                else:
                    raise DocumentServiceError(
                        "stale_source", "source changed during Undo recovery", status_code=409
                    )
                backup_path = str(backup)
                after_sha256 = written.sha256
        self._record_recovered_file_applied(
            execution,
            after_sha256=after_sha256,
            backup_path=backup_path,
            recovery_path=recovery_path,
        )

    def _record_recovered_file_applied(
        self,
        execution: dict[str, Any],
        *,
        after_sha256: str | None,
        backup_path: str | None,
        recovery_path: str | None,
    ) -> None:
        event_id = "devent_" + uuid.uuid4().hex
        payload = {
            "schema_version": "2.0.0",
            "event_id": event_id,
            "execution_id": execution["id"],
            "document_id": execution["document_id"],
            "source_uri": execution["source_uri"],
            "change_kind": execution["change_kind"],
            "format": execution["format"],
            "capability_id": execution["capability_id"],
            "before_sha256": execution["before_sha256"],
            "after_sha256": after_sha256,
            "artifact_sha256": execution["artifact_sha256"],
            "expected_source_sha256": after_sha256,
        }
        self._state.mark_file_applied(
            execution_id=str(execution["id"]),
            backup_path=backup_path,
            recovery_path=recovery_path,
            event_id=event_id,
            event_payload=payload,
        )
        self._sync_notifier()

    async def _apply_undo(self, undo_id: str, original: dict[str, Any]) -> None:
        document_id = str(original["document_id"])
        source_uri = str(original["source_uri"])
        recovery_path: str | None = None
        backup_path: str | None = None
        async with self._locks.acquire(document_id):
            current = await asyncio.to_thread(self._files.snapshot, source_uri)
            if current.sha256 != original["after_sha256"]:
                raise DocumentServiceError(
                    "stale_source", "source changed after execution", status_code=409
                )
            if original["change_kind"] in {"create", "derive"}:
                recovery = await asyncio.to_thread(
                    self._files.move_to_recovery,
                    source_uri=source_uri,
                    execution_id=undo_id,
                )
                recovery_path = str(recovery)
                after_sha = None
                change_kind = "recovery_remove"
            else:
                if not original["backup_path"]:
                    raise DocumentServiceError(
                        "backup_not_found", "immutable backup is missing", status_code=409
                    )
                backup_content = await asyncio.to_thread(
                    self._files.read_backup,
                    original["backup_path"],
                )
                snapshot = await asyncio.to_thread(
                    self._files.write_backup,
                    undo_id,
                    current,
                )
                backup_path = str(snapshot)
                written = await asyncio.to_thread(
                    self._files.atomic_replace,
                    source_uri=source_uri,
                    expected_sha256=current.sha256,
                    new_content=backup_content,
                )
                after_sha = written.sha256
                change_kind = "update"
        event_id = "devent_" + uuid.uuid4().hex
        payload = {
            "schema_version": "2.0.0",
            "event_id": event_id,
            "execution_id": undo_id,
            "document_id": document_id,
            "source_uri": source_uri,
            "change_kind": change_kind,
            "format": original["format"],
            "capability_id": original["capability_id"],
            "before_sha256": original["after_sha256"],
            "after_sha256": after_sha,
            "artifact_sha256": original["artifact_sha256"],
            "expected_source_sha256": after_sha,
        }
        self._state.mark_file_applied(
            execution_id=undo_id,
            backup_path=backup_path,
            recovery_path=recovery_path,
            event_id=event_id,
            event_payload=payload,
        )
        self._sync_notifier()

    def _owned_plan(
        self,
        plan_id: str,
        access_context: AccessContext,
    ) -> StoredDocumentPlan:
        try:
            plan = self._state.get_plan(plan_id)
        except DocumentStateError as error:
            raise _state_error(error) from error
        _require_owner(plan.values, access_context, "plan_not_found")
        return plan

    def _authorize_plan(self, plan: StoredDocumentPlan, access_context: AccessContext) -> None:
        if plan.values["kind"] == PlanKind.MUTATION:
            self._manifest(str(plan.values["document_id"]), access_context, write=True)
        else:
            _require_creation_access(str(plan.values["document_id"]), access_context)
            if plan.values["kind"] == PlanKind.DERIVATION:
                payload = HwpDerivationPayload.model_validate(plan.values["operations"][0])
                self._manifest(payload.source_document_id, access_context, write=False)

    def _manifest(
        self,
        document_id: str,
        access_context: AccessContext,
        *,
        write: bool,
    ) -> Any:
        manifest = self._catalog.snapshot().get_manifest(
            document_id,
            access_context=access_context,
        )
        if manifest is None or (write and not access_context.can_write(manifest)):
            raise DocumentServiceError("document_not_found", "document not found", status_code=404)
        return manifest

    def _require_capability(
        self,
        *,
        capability_id: str,
        format_: DocumentFormat,
        operation_types: Sequence[str],
        snapshot_id: str,
    ) -> None:
        try:
            self._capabilities.require(
                capability_id=capability_id,
                format_=format_,
                operation_types=operation_types,
                snapshot_id=snapshot_id,
            )
        except CapabilityUnavailable as error:
            status_code = 503 if error.retryable else 422
            if error.code == "capability_snapshot_stale":
                status_code = 409
            raise DocumentServiceError(
                error.code,
                str(error),
                status_code=status_code,
                retryable=error.retryable,
            ) from error

    def _proposed_artifact(self, plan: StoredDocumentPlan) -> dict[str, Any]:
        artifact = next((item for item in plan.artifacts if item["kind"] == "proposed"), None)
        if artifact is None:
            raise DocumentServiceError(
                "artifact_not_found", "proposed artifact is missing", status_code=409
            )
        return artifact

    def _verified_artifact(self, artifact: dict[str, Any]) -> Path:
        path = Path(str(artifact["path"]))
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self._artifact_root)
        except (OSError, ValueError) as error:
            raise DocumentServiceError(
                "artifact_not_found", "artifact path is unsafe", status_code=409
            ) from error
        if not resolved.is_file() or resolved.is_symlink():
            raise DocumentServiceError("artifact_not_found", "artifact is unsafe", status_code=409)
        content = resolved.read_bytes()
        if (
            len(content) != artifact["byte_size"]
            or hashlib.sha256(content).hexdigest() != artifact["sha256"]
        ):
            raise DocumentServiceError(
                "artifact_hash_conflict", "artifact hash mismatch", status_code=409
            )
        return resolved

    def _write_artifact(
        self,
        path: Path,
        content: bytes,
        *,
        kind: str,
        mime_type: str,
        preview_label: str | None = None,
    ) -> dict[str, Any]:
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        fsync_file(path)
        fsync_directory(path.parent)
        return {
            "id": "artifact_" + uuid.uuid4().hex,
            "kind": kind,
            "path": str(path.resolve()),
            "sha256": hashlib.sha256(content).hexdigest(),
            "mime_type": mime_type,
            "byte_size": len(content),
            "preview_label": preview_label,
        }

    def _plan_root(self, plan_id: str) -> Path:
        root = (self._artifact_root / plan_id).resolve()
        if not root.is_relative_to(self._artifact_root):
            raise DocumentServiceError("unsafe_artifact_path", "unsafe plan ID", status_code=422)
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _cleanup_plan_artifacts(self, plan_id: str) -> None:
        root = self._plan_root(plan_id)
        if root.is_dir():
            shutil.rmtree(root)

    def _fail_preparation(self, plan_id: str, error: Exception) -> None:
        code = getattr(error, "code", "preparation_failed")
        retryable = bool(getattr(error, "retryable", False))
        self._state.fail_plan(plan_id, code=str(code), message=str(error), retryable=retryable)

    def _spawn(self, coroutine: Coroutine[Any, Any, None], *, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _validate_operation_provenance(operations: list[Any], structure: list[Any]) -> None:
    indexed = {item.locator.model_dump_json(exclude_none=True): item.value for item in structure}
    for operation in operations:
        if isinstance(operation, SpreadsheetCellsSetOperation):
            for cell in operation.cells:
                key = json.dumps(
                    {
                        "kind": "spreadsheet_cell",
                        "sheet_name": cell.sheet_name,
                        "address": cell.address,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                matches = [
                    value for locator, value in indexed.items() if _locator_subset(key, locator)
                ]
                if len(matches) != 1 or matches[0] != cell.expected.model_dump(mode="json"):
                    raise DocumentServiceError(
                        "locator_provenance_invalid",
                        "cell locator was not present in current source structure: "
                        f"{cell.sheet_name}!{cell.address}",
                        status_code=422,
                    )
        elif isinstance(operation, TextReplaceOperation | TableCellSetOperation):
            key = operation.locator.model_dump_json(exclude_none=True)
            if key not in indexed or operation.expected not in str(indexed[key]):
                raise DocumentServiceError(
                    "locator_provenance_invalid",
                    "operation locator and expected value were not present "
                    "in current source structure",
                    status_code=422,
                )
        elif hasattr(operation, "locator"):
            key = operation.locator.model_dump_json(exclude_none=True)
            if key not in indexed:
                raise DocumentServiceError(
                    "locator_provenance_invalid",
                    "operation locator was not present in current source structure",
                    status_code=422,
                )


def _locator_subset(expected: str, actual: str) -> bool:
    expected_values = json.loads(expected)
    actual_values = json.loads(actual)
    return all(actual_values.get(key) == value for key, value in expected_values.items())


def _plan_view(stored: StoredDocumentPlan) -> DocumentPlanView:
    value = stored.values
    artifacts = [
        ArtifactView(
            artifact_id=item["id"],
            kind=item["kind"],
            sha256=item["sha256"],
            mime_type=item["mime_type"],
            byte_size=item["byte_size"],
            preview_label=item["preview_label"],
        )
        for item in stored.artifacts
    ]
    error = None
    if value["error_code"]:
        error = {
            "code": value["error_code"],
            "message": value["error_message"],
            "retryable": bool(value["error_retryable"]),
        }
    return DocumentPlanView(
        change_plan_id=value["id"],
        kind=value["kind"],
        document_id=value["document_id"],
        format=value["format"],
        capability_id=value["capability_id"],
        source_uri=value["source_uri"],
        target_relative_path=value["target_relative_path"],
        status=value["status"],
        base_sha256=value["base_sha256"],
        proposed_sha256=value["proposed_sha256"],
        plan_hash=value["plan_hash"],
        graph_version=value["graph_version"],
        capability_snapshot_id=value["capability_snapshot_id"],
        writer_fingerprint=value["writer_fingerprint"],
        renderer_fingerprint=value["renderer_fingerprint"],
        operations=value["operations"],
        structural_diff=[StructuralDiff.model_validate(item) for item in value["structural_diff"]],
        artifacts=artifacts,
        preview_manifest=(
            PreviewManifest.model_validate(value["preview_manifest"])
            if value["preview_manifest"]
            else None
        ),
        warnings=value["warnings"] or [],
        error=error,
        created_at=datetime.fromisoformat(value["created_at"]),
        expires_at=datetime.fromisoformat(value["expires_at"]),
        updated_at=datetime.fromisoformat(value["updated_at"]),
    )


def _execution_view(stored: StoredDocumentExecution) -> DocumentExecutionView:
    value = stored.values
    error = None
    if value["error_code"]:
        error = {
            "code": value["error_code"],
            "message": value["error_message"],
            "retryable": bool(value["error_retryable"]),
        }
    return DocumentExecutionView(
        execution_id=value["id"],
        change_plan_id=value["plan_id"],
        undo_of_execution_id=value["undo_of_execution_id"],
        document_id=value["document_id"],
        change_kind=value["change_kind"],
        format=value["format"],
        capability_id=value["capability_id"],
        source_uri=value["source_uri"],
        status=value["status"],
        before_sha256=value["before_sha256"],
        after_sha256=value["after_sha256"],
        artifact_sha256=value["artifact_sha256"],
        graph_version_before=value["graph_version_before"],
        graph_version_after=value["graph_version_after"],
        error=error,
        created_at=datetime.fromisoformat(value["created_at"]),
        updated_at=datetime.fromisoformat(value["updated_at"]),
    )


def _format_from_uri(source_uri: str) -> DocumentFormat:
    suffix = PurePosixPath(source_uri.split("source://", 1)[-1]).suffix.lower().lstrip(".")
    try:
        return DocumentFormat(suffix)
    except ValueError as error:
        raise DocumentServiceError(
            "unsupported_format",
            f"unsupported document format: {suffix or 'none'}",
            status_code=422,
        ) from error


def _mime_type(format_: DocumentFormat) -> str:
    return {
        DocumentFormat.HWPX: "application/vnd.hancom.hwpx",
        DocumentFormat.DOCX: (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        DocumentFormat.PPTX: (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        ),
        DocumentFormat.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        DocumentFormat.PDF: "application/pdf",
        DocumentFormat.HWP: "application/x-hwp",
    }[format_]


def _selected_pages(diffs: list[StructuralDiff], format_: DocumentFormat) -> set[int] | None:
    if format_ not in {DocumentFormat.PPTX, DocumentFormat.PDF}:
        return None
    indices: set[int] = set()
    for diff in diffs:
        if diff.locator is None:
            continue
        index = (
            diff.locator.slide_index if format_ is DocumentFormat.PPTX else diff.locator.page_index
        )
        if index is not None:
            indices.add(index)
    return indices or None


def _hash_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _identity(access_context: AccessContext) -> tuple[str, str]:
    if (
        not access_context.provisioned
        or not access_context.tenant_id
        or not access_context.subject_id
    ):
        raise DocumentServiceError(
            "authentication_required", "authentication required", status_code=401
        )
    return access_context.tenant_id, access_context.subject_id


def _require_creation_access(document_id: str, access_context: AccessContext) -> None:
    _identity(access_context)
    if not (access_context.allow_all_writes or document_id in access_context.writable_document_ids):
        raise DocumentServiceError("document_not_found", "document not found", status_code=404)


def _require_owner(values: dict[str, Any], access_context: AccessContext, code: str) -> None:
    tenant_id, subject_id = _identity(access_context)
    if values["tenant_id"] != tenant_id or values["subject_id"] != subject_id:
        raise DocumentServiceError(code, "resource not found", status_code=404)


def _state_error(error: DocumentStateError) -> DocumentServiceError:
    status = 404 if error.code.endswith("not_found") else 409
    return DocumentServiceError(error.code, str(error), status_code=status)
