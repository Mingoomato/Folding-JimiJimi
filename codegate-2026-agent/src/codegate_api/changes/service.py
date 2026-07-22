from __future__ import annotations

import difflib
import hashlib
import secrets
import sqlite3
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from pydantic import ValidationError

from codegate_api.changes.hashing import change_plan_hash, request_hash
from codegate_api.config import Settings
from codegate_api.files.atomic import (
    DocumentLockManager,
    FileHashConflict,
    SafeFileError,
    SafeSourceFileStore,
)
from codegate_api.knowledge.access import AccessContext
from codegate_api.knowledge.pipeline import (
    ContentChangedPayload,
    SyncPipelineError,
)
from codegate_api.knowledge.repository import KnowledgeRepository
from codegate_api.models import (
    ChangePlanStatus,
    ChangePlanView,
    ExecutionStatus,
    ExecutionView,
    FileStatus,
    RejectChangePlanResponse,
    ReplaceExactOperation,
    SyncStatus,
)
from codegate_api.state.store import (
    StateConflict,
    StateNotFound,
    StateStore,
    StoredExecution,
)


class KnowledgeCatalogSnapshot(Protocol):
    def snapshot(self) -> KnowledgeRepository: ...


class ChangeSyncPipeline(Protocol):
    async def process_pending(self) -> str: ...


class ChangeServiceError(RuntimeError):
    def __init__(self, code: str, message: str, *, conflict: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.conflict = conflict


class ChangePlanService:
    def __init__(
        self,
        *,
        settings: Settings,
        file_store: SafeSourceFileStore,
        state_store: StateStore,
    ) -> None:
        self._settings = settings
        self._files = file_store
        self._state = state_store

    def create_plan(
        self,
        *,
        repository: KnowledgeRepository,
        access_context: AccessContext,
        document_id: str,
        operation: ReplaceExactOperation,
        request_text: str,
        change_plan_id: str | None = None,
        expected_base_sha256: str | None = None,
        expected_proposed_content: bytes | None = None,
    ) -> ChangePlanView:
        tenant_id, subject_id = _require_principal(access_context)
        if change_plan_id is not None:
            try:
                existing = self._state.get_plan(
                    change_plan_id,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                ).view
            except StateNotFound:
                pass
            else:
                if _matches_source_sync_request(
                    existing,
                    document_id=document_id,
                    operation=operation,
                    base_sha256=expected_base_sha256,
                    proposed_content=expected_proposed_content,
                ):
                    return existing
                raise ChangeServiceError(
                    "IDEMPOTENCY_KEY_REUSED",
                    "같은 Idempotency-Key를 다른 source sync 요청에 다시 사용할 수 없습니다.",
                    conflict=True,
                )
        manifest = repository.get_manifest(document_id, access_context=access_context)
        if manifest is None or not access_context.can_write(manifest):
            raise ChangeServiceError("DOCUMENT_NOT_FOUND", "수정 가능한 문서를 찾을 수 없습니다.")
        suffix = _source_suffix(manifest.source.uri)
        if suffix not in {".md", ".markdown", ".txt"} or manifest.source.media_type not in {
            "text/markdown",
            "text/plain",
        }:
            raise ChangeServiceError(
                "UNSUPPORTED_WRITE_FORMAT",
                "현재 실제 쓰기는 UTF-8 Markdown/TXT만 지원합니다.",
            )
        if not repository.supports_exact_change(document_id, operation.expected_text):
            raise ChangeServiceError(
                "CHANGE_NOT_INDEXABLE",
                "현재 변환·근거 계약으로 안전하게 재색인할 수 없는 변경입니다.",
                conflict=True,
            )
        try:
            snapshot = self._files.snapshot(manifest.source.uri)
        except SafeFileError as error:
            raise ChangeServiceError(error.code, str(error)) from error
        if snapshot.sha256 != manifest.source.sha256:
            raise ChangeServiceError(
                "SOURCE_OUT_OF_SYNC",
                "원본 hash와 활성 지식 버전이 다릅니다. 먼저 동기화해야 합니다.",
                conflict=True,
            )
        if expected_base_sha256 is not None and snapshot.sha256 != expected_base_sha256:
            raise ChangeServiceError(
                "SOURCE_HASH_CONFLICT",
                "업로드 기준 hash와 현재 원본이 다릅니다.",
                conflict=True,
            )
        try:
            current_text = snapshot.content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ChangeServiceError(
                "UNSUPPORTED_ENCODING", "UTF-8 문서만 수정할 수 있습니다."
            ) from error
        if operation.expected_text == operation.replacement_text:
            raise ChangeServiceError("NO_CHANGE", "변경 전후 텍스트가 같습니다.")
        if current_text.count(operation.expected_text) != operation.expected_occurrences:
            raise ChangeServiceError(
                "ANCHOR_NOT_UNIQUE",
                "변경할 텍스트가 정확히 한 번 존재해야 합니다.",
                conflict=True,
            )
        if current_text.count(operation.replacement_text) != 0:
            raise ChangeServiceError(
                "REPLACEMENT_ALREADY_PRESENT",
                "Undo를 안전하게 보장하려면 변경 후 텍스트가 원본에 없어야 합니다.",
                conflict=True,
            )
        proposed_text = current_text.replace(
            operation.expected_text,
            operation.replacement_text,
            1,
        )
        proposed_content = proposed_text.encode("utf-8")
        if expected_proposed_content is not None and proposed_content != expected_proposed_content:
            raise ChangeServiceError(
                "SOURCE_SYNC_CONTENT_MISMATCH",
                "업로드 본문이 선언한 exact operation 결과와 다릅니다.",
                conflict=True,
            )
        if len(proposed_content) > self._settings.max_edit_bytes:
            raise ChangeServiceError("FILE_TOO_LARGE", "수정 결과가 파일 크기 제한을 초과합니다.")
        now = datetime.now(UTC)
        unified_diff = "".join(
            difflib.unified_diff(
                current_text.splitlines(keepends=True),
                proposed_text.splitlines(keepends=True),
                fromfile=f"{manifest.source.uri}@{snapshot.sha256[:12]}",
                tofile=f"{manifest.source.uri}@proposed",
            )
        )
        provisional = ChangePlanView(
            change_plan_id=change_plan_id or f"plan_{uuid4().hex}",
            document_id=document_id,
            file_version_id=manifest.file_version_id,
            source_uri=manifest.source.uri,
            operation=operation,
            unified_diff=unified_diff,
            base_sha256=snapshot.sha256,
            proposed_sha256=_sha256(proposed_content),
            plan_hash="0" * 64,
            status=ChangePlanStatus.PENDING_APPROVAL,
            created_at=now,
            expires_at=now + timedelta(seconds=self._settings.plan_ttl_seconds),
        )
        plan = provisional.model_copy(update={"plan_hash": change_plan_hash(provisional)})
        try:
            self._state.create_plan(
                plan,
                tenant_id=tenant_id,
                subject_id=subject_id,
                request_text=request_text,
            )
        except sqlite3.IntegrityError:
            if change_plan_id is None:
                raise
            existing = self._state.get_plan(
                change_plan_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
            ).view
            if _matches_source_sync_request(
                existing,
                document_id=document_id,
                operation=operation,
                base_sha256=expected_base_sha256,
                proposed_content=expected_proposed_content,
            ):
                return existing
            raise ChangeServiceError(
                "IDEMPOTENCY_KEY_REUSED",
                "같은 Idempotency-Key를 다른 source sync 요청에 다시 사용할 수 없습니다.",
                conflict=True,
            ) from None
        return plan

    def create_source_sync_plan(
        self,
        *,
        repository: KnowledgeRepository,
        access_context: AccessContext,
        document_id: str,
        operation: ReplaceExactOperation,
        base_sha256: str,
        content: str,
        idempotency_key: str,
    ) -> ChangePlanView:
        tenant_id, subject_id = _require_principal(access_context)
        plan_id = (
            "plan_sync_"
            + _sha256("\x00".join([tenant_id, subject_id, document_id, idempotency_key]).encode())[
                :32
            ]
        )
        return self.create_plan(
            repository=repository,
            access_context=access_context,
            document_id=document_id,
            operation=operation,
            request_text="authenticated source sync",
            change_plan_id=plan_id,
            expected_base_sha256=base_sha256,
            expected_proposed_content=content.encode("utf-8"),
        )


class ChangeExecutionService:
    def __init__(
        self,
        *,
        catalog: KnowledgeCatalogSnapshot,
        file_store: SafeSourceFileStore,
        state_store: StateStore,
        lock_manager: DocumentLockManager,
        pipeline: ChangeSyncPipeline,
        sync_notifier: Callable[[], None] | None = None,
    ) -> None:
        self._catalog = catalog
        self._files = file_store
        self._state = state_store
        self._locks = lock_manager
        self._pipeline = pipeline
        self._sync_notifier = sync_notifier or (lambda: None)

    async def approve(
        self,
        *,
        plan_id: str,
        supplied_plan_hash: str,
        access_context: AccessContext,
        idempotency_key: str,
    ) -> ExecutionView:
        tenant_id, subject_id = _require_principal(access_context)
        scope = f"POST:/api/v1/change-plans/{plan_id}/approve"
        approval_request_hash = request_hash({"plan_hash": supplied_plan_hash})
        try:
            replay = self._state.idempotent_execution(
                scope=scope,
                tenant_id=tenant_id,
                subject_id=subject_id,
                key=idempotency_key,
                request_hash=approval_request_hash,
            )
        except StateConflict as error:
            raise _state_error(error) from error
        if replay is not None:
            if replay.file_status is not FileStatus.FAILED:
                return replay
            stored_replay = self._state.get_execution(
                replay.execution_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
            )
            async with self._locks.acquire(replay.document_id):
                return await self._resume_aborted_write_locked(
                    execution=stored_replay,
                    access_context=access_context,
                    idempotency_scope=scope,
                    idempotency_key=idempotency_key,
                    request_hash=approval_request_hash,
                )
        try:
            plan = self._state.get_plan(plan_id, tenant_id=tenant_id, subject_id=subject_id)
        except StateNotFound as error:
            raise ChangeServiceError(
                "CHANGE_PLAN_NOT_FOUND", "변경안을 찾을 수 없습니다."
            ) from error
        _verify_plan(plan.view, supplied_plan_hash)

        async with self._locks.acquire(plan.view.document_id):
            try:
                replay = self._state.idempotent_execution(
                    scope=scope,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    key=idempotency_key,
                    request_hash=approval_request_hash,
                )
            except StateConflict as error:
                raise _state_error(error) from error
            if replay is not None:
                if replay.file_status is not FileStatus.FAILED:
                    return replay
                stored_replay = self._state.get_execution(
                    replay.execution_id,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                )
                return await self._resume_aborted_write_locked(
                    execution=stored_replay,
                    access_context=access_context,
                    idempotency_scope=scope,
                    idempotency_key=idempotency_key,
                    request_hash=approval_request_hash,
                )
            if self._state.has_failed_sync_events():
                raise ChangeServiceError(
                    "KNOWLEDGE_SYNC_BLOCKED",
                    "실패한 지식 동기화를 retry 또는 Undo로 먼저 해소해야 합니다.",
                    conflict=True,
                )
            plan = self._state.get_plan(plan_id, tenant_id=tenant_id, subject_id=subject_id)
            _verify_plan(plan.view, supplied_plan_hash)
            existing_execution = self._state.execution_for_plan(
                plan_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
            )
            if existing_execution is not None:
                if existing_execution.view.file_status is FileStatus.FAILED:
                    return await self._resume_aborted_write_locked(
                        execution=existing_execution,
                        access_context=access_context,
                        idempotency_scope=scope,
                        idempotency_key=idempotency_key,
                        request_hash=approval_request_hash,
                    )
                raise ChangeServiceError(
                    "PLAN_NOT_PENDING",
                    "이미 처리된 변경안입니다.",
                    conflict=True,
                )
            if plan.view.status is not ChangePlanStatus.PENDING_APPROVAL:
                raise ChangeServiceError(
                    "PLAN_NOT_PENDING",
                    "이미 처리된 변경안입니다.",
                    conflict=True,
                )
            repository = self._catalog.snapshot()
            manifest = repository.get_manifest(
                plan.view.document_id,
                access_context=access_context,
            )
            if manifest is None or not access_context.can_write(manifest):
                raise ChangeServiceError(
                    "DOCUMENT_NOT_FOUND", "수정 가능한 문서를 찾을 수 없습니다."
                )
            if manifest.source.uri != plan.view.source_uri:
                raise ChangeServiceError(
                    "SOURCE_MOVED", "원본 위치가 변경되었습니다.", conflict=True
                )
            try:
                snapshot = self._files.snapshot(plan.view.source_uri)
            except SafeFileError as error:
                raise ChangeServiceError(error.code, str(error)) from error
            if snapshot.sha256 != plan.view.base_sha256:
                raise ChangeServiceError(
                    "SOURCE_HASH_CONFLICT",
                    "원본 파일이 변경안 생성 이후 바뀌었습니다.",
                    conflict=True,
                )
            new_content = _apply_operation(snapshot.content, plan.view.operation)
            if _sha256(new_content) != plan.view.proposed_sha256:
                raise ChangeServiceError(
                    "PLAN_INTEGRITY_ERROR", "변경안 결과 hash가 일치하지 않습니다."
                )

            execution_id = f"exec_{uuid4().hex}"
            event_id = f"evt_{uuid4().hex}"
            file_version_id = f"fv_{uuid4().hex}"
            try:
                backup_path = self._files.write_backup(execution_id, snapshot)
            except (SafeFileError, OSError) as error:
                code = error.code if isinstance(error, SafeFileError) else "BACKUP_WRITE_FAILED"
                raise ChangeServiceError(code, str(error)) from error
            now = datetime.now(UTC)
            execution = ExecutionView(
                execution_id=execution_id,
                change_plan_id=plan_id,
                undo_of_execution_id=None,
                document_id=plan.view.document_id,
                status=ExecutionStatus.PREPARED,
                file_status=FileStatus.PENDING,
                sync_status=SyncStatus.PENDING,
                before_sha256=snapshot.sha256,
                after_sha256=plan.view.proposed_sha256,
                file_version_id=None,
                graph_version_before=repository.version,
                graph_version_after=None,
                created_at=now,
                updated_at=now,
            )
            payload = _event_payload(
                event_id=event_id,
                execution_id=execution_id,
                document_id=plan.view.document_id,
                source_uri=plan.view.source_uri,
                before_sha256=snapshot.sha256,
                after_sha256=plan.view.proposed_sha256,
                file_version_id=file_version_id,
                operation=plan.view.operation,
            )
            try:
                prepared = self._state.prepare_execution(
                    plan=plan,
                    execution=execution,
                    backup_path=str(backup_path),
                    event_id=event_id,
                    file_version_id=file_version_id,
                    event_payload=payload,
                    approval_id=f"approval_{uuid4().hex}",
                    idempotency_key=idempotency_key,
                    request_hash=approval_request_hash,
                )
            except StateConflict as error:
                raise _state_error(error) from error
            if prepared.execution_id != execution_id:
                return prepared
            await self._apply_prepared_write(
                execution_id=execution_id,
                source_uri=plan.view.source_uri,
                before_sha256=snapshot.sha256,
                after_sha256=plan.view.proposed_sha256,
                new_content=new_content,
            )
            return self._state.get_execution(
                execution_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
            ).view

    def reject(
        self,
        *,
        plan_id: str,
        supplied_plan_hash: str,
        reason: str | None,
        access_context: AccessContext,
        idempotency_key: str,
    ) -> RejectChangePlanResponse:
        tenant_id, subject_id = _require_principal(access_context)
        try:
            plan = self._state.get_plan(plan_id, tenant_id=tenant_id, subject_id=subject_id)
        except StateNotFound as error:
            raise ChangeServiceError(
                "CHANGE_PLAN_NOT_FOUND", "변경안을 찾을 수 없습니다."
            ) from error
        _verify_plan(plan.view, supplied_plan_hash)
        body_hash = request_hash({"plan_hash": supplied_plan_hash, "reason": reason})
        try:
            result = self._state.reject_plan(
                plan_id=plan_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                plan_hash=supplied_plan_hash,
                reason=reason,
                idempotency_key=idempotency_key,
                request_hash=body_hash,
                approval_id=f"approval_{uuid4().hex}",
            )
        except StateConflict as error:
            raise _state_error(error) from error
        return RejectChangePlanResponse(change_plan_id=plan_id, status=result)

    def get_execution(
        self,
        execution_id: str,
        *,
        access_context: AccessContext,
    ) -> ExecutionView:
        tenant_id, subject_id = _require_principal(access_context)
        try:
            return self._state.get_execution(
                execution_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
            ).view
        except StateNotFound as error:
            raise ChangeServiceError("EXECUTION_NOT_FOUND", "실행을 찾을 수 없습니다.") from error

    def retry_sync(
        self,
        execution_id: str,
        *,
        access_context: AccessContext,
    ) -> ExecutionView:
        execution = self.get_execution(execution_id, access_context=access_context)
        if (
            execution.file_status is not FileStatus.APPLIED
            or execution.sync_status is not SyncStatus.FAILED
        ):
            raise ChangeServiceError(
                "SYNC_RETRY_NOT_AVAILABLE",
                "파일 적용은 끝났지만 동기화가 실패한 실행만 재시도할 수 있습니다.",
                conflict=True,
            )
        try:
            self._state.queue_failed_execution(execution_id)
        except StateConflict as error:
            raise _state_error(error) from error
        self._sync_notifier()
        return self.get_execution(execution_id, access_context=access_context)

    async def undo(
        self,
        *,
        execution_id: str,
        access_context: AccessContext,
        idempotency_key: str,
    ) -> ExecutionView:
        tenant_id, subject_id = _require_principal(access_context)
        scope = f"POST:/api/v1/executions/{execution_id}/undo"
        undo_request_hash = request_hash({"execution_id": execution_id, "action": "undo"})
        try:
            replay = self._state.idempotent_execution(
                scope=scope,
                tenant_id=tenant_id,
                subject_id=subject_id,
                key=idempotency_key,
                request_hash=undo_request_hash,
            )
        except StateConflict as error:
            raise _state_error(error) from error
        if replay is not None:
            if replay.file_status is not FileStatus.FAILED:
                return replay
            stored_replay = self._state.get_execution(
                replay.execution_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
            )
            async with self._locks.acquire(replay.document_id):
                return await self._resume_aborted_write_locked(
                    execution=stored_replay,
                    access_context=access_context,
                    idempotency_scope=scope,
                    idempotency_key=idempotency_key,
                    request_hash=undo_request_hash,
                )
        try:
            original = self._state.get_execution(
                execution_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
            )
        except StateNotFound as error:
            raise ChangeServiceError("EXECUTION_NOT_FOUND", "실행을 찾을 수 없습니다.") from error
        original_plan_id = original.view.change_plan_id
        if original_plan_id is None:
            raise ChangeServiceError("UNDO_NOT_AVAILABLE", "Undo 실행은 다시 되돌릴 수 없습니다.")

        async with self._locks.acquire(original.view.document_id):
            try:
                replay = self._state.idempotent_execution(
                    scope=scope,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    key=idempotency_key,
                    request_hash=undo_request_hash,
                )
            except StateConflict as error:
                raise _state_error(error) from error
            if replay is not None:
                if replay.file_status is not FileStatus.FAILED:
                    return replay
                stored_replay = self._state.get_execution(
                    replay.execution_id,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                )
                return await self._resume_aborted_write_locked(
                    execution=stored_replay,
                    access_context=access_context,
                    idempotency_scope=scope,
                    idempotency_key=idempotency_key,
                    request_hash=undo_request_hash,
                )
            original = self._state.get_execution(
                execution_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
            )
            existing_undo = self._state.undo_execution_for(
                execution_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
            )
            if existing_undo is not None:
                if existing_undo.view.file_status is FileStatus.FAILED:
                    return await self._resume_aborted_write_locked(
                        execution=existing_undo,
                        access_context=access_context,
                        idempotency_scope=scope,
                        idempotency_key=idempotency_key,
                        request_hash=undo_request_hash,
                    )
                raise ChangeServiceError(
                    "UNDO_ALREADY_USED",
                    "이미 되돌린 실행입니다.",
                    conflict=True,
                )
            if not (
                original.view.status is ExecutionStatus.COMPLETED
                or (
                    original.view.status is ExecutionStatus.FILE_APPLIED
                    and original.view.sync_status is SyncStatus.FAILED
                )
            ):
                raise ChangeServiceError(
                    "UNDO_NOT_AVAILABLE",
                    "완료됐거나 동기화 실패 후 원본이 유지된 실행만 되돌릴 수 있습니다.",
                    conflict=True,
                )
            repository = self._catalog.snapshot()
            manifest = repository.get_manifest(
                original.view.document_id,
                access_context=access_context,
            )
            if manifest is None or not access_context.can_write(manifest):
                raise ChangeServiceError(
                    "DOCUMENT_NOT_FOUND", "수정 가능한 문서를 찾을 수 없습니다."
                )
            try:
                current = self._files.snapshot(original.source_uri)
            except SafeFileError as error:
                raise ChangeServiceError(error.code, str(error)) from error
            if current.sha256 != original.view.after_sha256:
                raise ChangeServiceError(
                    "UNDO_HASH_CONFLICT",
                    "실행 이후 원본이 바뀌어 안전하게 되돌릴 수 없습니다.",
                    conflict=True,
                )
            try:
                backup_content = self._files.read_backup(original.backup_path)
            except SafeFileError as error:
                raise ChangeServiceError(error.code, str(error)) from error
            if _sha256(backup_content) != original.view.before_sha256:
                raise ChangeServiceError("BACKUP_INTEGRITY_ERROR", "백업 hash가 일치하지 않습니다.")
            plan = self._state.get_plan(
                original_plan_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
            )
            inverse = ReplaceExactOperation(
                expected_text=plan.view.operation.replacement_text,
                replacement_text=plan.view.operation.expected_text,
            )
            if _apply_operation(current.content, inverse) != backup_content:
                raise ChangeServiceError("UNDO_INTEGRITY_ERROR", "역변경 결과가 백업과 다릅니다.")

            undo_execution_id = f"exec_{uuid4().hex}"
            event_id = f"evt_{uuid4().hex}"
            file_version_id = f"fv_{uuid4().hex}"
            try:
                undo_backup_path = self._files.write_backup(undo_execution_id, current)
            except (SafeFileError, OSError) as error:
                code = error.code if isinstance(error, SafeFileError) else "BACKUP_WRITE_FAILED"
                raise ChangeServiceError(code, str(error)) from error
            now = datetime.now(UTC)
            undo_execution = ExecutionView(
                execution_id=undo_execution_id,
                change_plan_id=None,
                undo_of_execution_id=execution_id,
                document_id=original.view.document_id,
                status=ExecutionStatus.PREPARED,
                file_status=FileStatus.PENDING,
                sync_status=SyncStatus.PENDING,
                before_sha256=current.sha256,
                after_sha256=original.view.before_sha256,
                file_version_id=None,
                graph_version_before=repository.version,
                graph_version_after=None,
                created_at=now,
                updated_at=now,
            )
            payload = _event_payload(
                event_id=event_id,
                execution_id=undo_execution_id,
                document_id=original.view.document_id,
                source_uri=original.source_uri,
                before_sha256=current.sha256,
                after_sha256=original.view.before_sha256,
                file_version_id=file_version_id,
                operation=inverse,
            )
            try:
                prepared = self._state.prepare_undo(
                    original=original,
                    execution=undo_execution,
                    backup_path=str(undo_backup_path),
                    event_id=event_id,
                    file_version_id=file_version_id,
                    event_payload=payload,
                    idempotency_key=idempotency_key,
                    request_hash=undo_request_hash,
                )
            except StateConflict as error:
                raise _state_error(error) from error
            if prepared.execution_id != undo_execution_id:
                return prepared
            await self._apply_prepared_write(
                execution_id=undo_execution_id,
                source_uri=original.source_uri,
                before_sha256=current.sha256,
                after_sha256=original.view.before_sha256,
                new_content=backup_content,
            )
            return self._state.get_execution(
                undo_execution_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
            ).view

    async def _resume_aborted_write_locked(
        self,
        *,
        execution: StoredExecution,
        access_context: AccessContext,
        idempotency_scope: str,
        idempotency_key: str,
        request_hash: str,
    ) -> ExecutionView:
        tenant_id, subject_id = _require_principal(access_context)
        execution = self._state.get_execution(
            execution.view.execution_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
        )
        if execution.view.file_status is not FileStatus.FAILED:
            return execution.view
        manifest = self._catalog.snapshot().get_manifest(
            execution.view.document_id,
            access_context=access_context,
        )
        if (
            manifest is None
            or not access_context.can_write(manifest)
            or manifest.source.uri != execution.source_uri
        ):
            raise ChangeServiceError("DOCUMENT_NOT_FOUND", "수정 가능한 문서를 찾을 수 없습니다.")
        try:
            journal = self._state.get_write_journal(
                execution.view.execution_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
            )
            payload = ContentChangedPayload.model_validate(journal.payload)
        except StateNotFound as error:
            raise ChangeServiceError(
                "WRITE_JOURNAL_NOT_FOUND", "재시도할 쓰기 저널을 찾을 수 없습니다."
            ) from error
        except ValidationError as error:
            raise ChangeServiceError(
                "WRITE_JOURNAL_INTEGRITY_ERROR", "쓰기 저널 payload가 손상되었습니다."
            ) from error
        if not (
            journal.state == "aborted"
            and payload.event_id == journal.event_id
            and payload.execution_id == execution.view.execution_id
            and payload.document_id == execution.view.document_id
            and payload.source_uri == execution.source_uri
            and payload.before_sha256 == execution.view.before_sha256
            and payload.after_sha256 == execution.view.after_sha256
            and payload.file_version_id == journal.file_version_id
        ):
            raise ChangeServiceError(
                "WRITE_JOURNAL_INTEGRITY_ERROR", "쓰기 저널과 실행 정보가 일치하지 않습니다."
            )
        try:
            current = self._files.snapshot(execution.source_uri)
        except SafeFileError as error:
            raise ChangeServiceError(error.code, str(error)) from error
        if current.sha256 not in {
            execution.view.before_sha256,
            execution.view.after_sha256,
        }:
            self._state.mark_execution_conflict(
                execution.view.execution_id,
                code="WRITE_RETRY_HASH_CONFLICT",
                message="쓰기 재시도 전에 원본이 다른 내용으로 바뀌었습니다.",
            )
            raise ChangeServiceError(
                "WRITE_RETRY_HASH_CONFLICT",
                "쓰기 재시도 전에 원본이 다른 내용으로 바뀌었습니다.",
                conflict=True,
            )
        if current.sha256 == execution.view.before_sha256:
            try:
                new_content = _apply_operation(current.content, payload.operation)
            except ChangeServiceError as error:
                raise ChangeServiceError(
                    "WRITE_JOURNAL_INTEGRITY_ERROR",
                    "쓰기 저널의 변경 연산을 원본에 적용할 수 없습니다.",
                ) from error
            if _sha256(new_content) != execution.view.after_sha256:
                raise ChangeServiceError(
                    "WRITE_JOURNAL_INTEGRITY_ERROR",
                    "쓰기 저널의 변경 결과 hash가 일치하지 않습니다.",
                )
        else:
            new_content = current.content
        try:
            self._state.rearm_aborted_write(
                execution.view.execution_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                idempotency_scope=idempotency_scope,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
        except StateConflict as error:
            raise _state_error(error) from error
        if current.sha256 == execution.view.after_sha256:
            self._state.mark_journal_replaced(execution.view.execution_id)
            self._state.finalize_file_write(execution.view.execution_id)
            self._sync_notifier()
        else:
            await self._apply_prepared_write(
                execution_id=execution.view.execution_id,
                source_uri=execution.source_uri,
                before_sha256=execution.view.before_sha256,
                after_sha256=execution.view.after_sha256,
                new_content=new_content,
            )
        return self._state.get_execution(
            execution.view.execution_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
        ).view

    async def recover(self) -> None:
        await self.recover_files()
        await self.recover_sync()

    async def recover_files(self) -> None:
        for journal in self._state.unfinished_journals():
            try:
                current = self._files.snapshot(journal.source_uri)
            except SafeFileError:
                self._state.mark_execution_conflict(
                    journal.execution_id,
                    code="RECOVERY_SOURCE_UNAVAILABLE",
                    message="재시작 복구 중 원본 파일을 안전하게 열 수 없습니다.",
                )
                continue
            if current.sha256 == journal.after_sha256:
                self._state.mark_journal_replaced(journal.execution_id)
                self._state.finalize_file_write(journal.execution_id)
            elif current.sha256 == journal.before_sha256:
                self._state.abort_prepared_write(
                    journal.execution_id,
                    code="WRITE_NOT_APPLIED",
                    message="재시작 복구에서 원본 미변경을 확인했습니다.",
                )
            else:
                self._state.mark_execution_conflict(
                    journal.execution_id,
                    code="RECOVERY_HASH_CONFLICT",
                    message="재시작 복구에서 예상하지 못한 원본 hash를 발견했습니다.",
                )

    async def recover_sync(self) -> None:
        with suppress(SyncPipelineError):
            await self._pipeline.process_pending()

    async def _apply_prepared_write(
        self,
        *,
        execution_id: str,
        source_uri: str,
        before_sha256: str,
        after_sha256: str,
        new_content: bytes,
    ) -> None:
        try:
            written = self._files.atomic_replace(
                source_uri=source_uri,
                expected_sha256=before_sha256,
                new_content=new_content,
            )
            if written.sha256 != after_sha256:
                raise ChangeServiceError("WRITE_VERIFY_FAILED", "쓰기 후 hash 검증에 실패했습니다.")
            self._state.mark_journal_replaced(execution_id)
            self._state.finalize_file_write(execution_id)
        except FileHashConflict as error:
            self._state.mark_execution_conflict(
                execution_id,
                code=error.code,
                message=str(error),
            )
            raise ChangeServiceError(error.code, str(error), conflict=True) from error
        except (SafeFileError, OSError, ChangeServiceError) as error:
            await self._recover_execution_after_error(
                execution_id, source_uri, before_sha256, after_sha256
            )
            if isinstance(error, ChangeServiceError):
                raise
            code = error.code if isinstance(error, SafeFileError) else "ATOMIC_WRITE_FAILED"
            raise ChangeServiceError(code, str(error)) from error

        self._sync_notifier()

    async def _recover_execution_after_error(
        self,
        execution_id: str,
        source_uri: str,
        before_sha256: str,
        after_sha256: str,
    ) -> None:
        try:
            current = self._files.snapshot(source_uri)
        except SafeFileError:
            self._state.mark_execution_conflict(
                execution_id,
                code="WRITE_RECOVERY_FAILED",
                message="쓰기 오류 뒤 원본 상태를 확인할 수 없습니다.",
            )
            return
        if current.sha256 == after_sha256:
            self._state.mark_journal_replaced(execution_id)
            self._state.finalize_file_write(execution_id)
        elif current.sha256 == before_sha256:
            self._state.abort_prepared_write(
                execution_id,
                code="ATOMIC_WRITE_FAILED",
                message="원본은 변경되지 않았습니다.",
            )
        else:
            self._state.mark_execution_conflict(
                execution_id,
                code="WRITE_RECOVERY_HASH_CONFLICT",
                message="쓰기 오류 뒤 예상하지 못한 원본 hash가 발견됐습니다.",
            )


def _verify_plan(plan: ChangePlanView, supplied_hash: str) -> None:
    stored_hash = change_plan_hash(plan)
    if not secrets.compare_digest(stored_hash, plan.plan_hash):
        raise ChangeServiceError("PLAN_INTEGRITY_ERROR", "저장된 변경안 hash가 손상되었습니다.")
    if not secrets.compare_digest(plan.plan_hash, supplied_hash):
        raise ChangeServiceError(
            "PLAN_HASH_MISMATCH",
            "승인 화면의 plan_hash와 다릅니다.",
            conflict=True,
        )
    if plan.status is ChangePlanStatus.PENDING_APPROVAL and plan.expires_at <= datetime.now(UTC):
        raise ChangeServiceError("PLAN_EXPIRED", "변경안이 만료되었습니다.", conflict=True)


def _matches_source_sync_request(
    plan: ChangePlanView,
    *,
    document_id: str,
    operation: ReplaceExactOperation,
    base_sha256: str | None,
    proposed_content: bytes | None,
) -> bool:
    if base_sha256 is None or proposed_content is None:
        return False
    return (
        plan.document_id == document_id
        and plan.operation == operation
        and secrets.compare_digest(plan.base_sha256, base_sha256)
        and secrets.compare_digest(plan.proposed_sha256, _sha256(proposed_content))
    )


def _require_principal(access_context: AccessContext) -> tuple[str, str]:
    if access_context.subject_id is None or access_context.tenant_id is None:
        raise ChangeServiceError("AUTHENTICATION_REQUIRED", "이 작업은 로그인이 필요합니다.")
    return access_context.tenant_id, access_context.subject_id


def _apply_operation(content: bytes, operation: ReplaceExactOperation) -> bytes:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ChangeServiceError(
            "UNSUPPORTED_ENCODING", "UTF-8 문서만 수정할 수 있습니다."
        ) from error
    if text.count(operation.expected_text) != operation.expected_occurrences:
        raise ChangeServiceError(
            "ANCHOR_NOT_UNIQUE",
            "변경할 텍스트가 정확히 한 번 존재해야 합니다.",
            conflict=True,
        )
    return text.replace(operation.expected_text, operation.replacement_text, 1).encode("utf-8")


def _event_payload(
    *,
    event_id: str,
    execution_id: str,
    document_id: str,
    source_uri: str,
    before_sha256: str,
    after_sha256: str,
    file_version_id: str,
    operation: ReplaceExactOperation,
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "event_id": event_id,
        "execution_id": execution_id,
        "document_id": document_id,
        "source_uri": source_uri,
        "before_sha256": before_sha256,
        "after_sha256": after_sha256,
        "file_version_id": file_version_id,
        "operation": operation.model_dump(mode="json"),
    }


def _source_suffix(source_uri: str) -> str:
    from pathlib import PurePosixPath
    from urllib.parse import urlparse

    parsed = urlparse(source_uri)
    return PurePosixPath(parsed.path).suffix.lower()


def _state_error(error: StateConflict) -> ChangeServiceError:
    return ChangeServiceError(error.code, str(error), conflict=True)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
