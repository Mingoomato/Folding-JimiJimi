from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from codegate_api.models import (
    ChangePlanStatus,
    ChangePlanView,
    ExecutionStatus,
    ExecutionView,
    FileStatus,
    ReplaceExactOperation,
    SyncStatus,
)


class StateConflict(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class StateNotFound(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StoredPlan:
    view: ChangePlanView
    tenant_id: str
    subject_id: str
    request_text: str


@dataclass(frozen=True, slots=True)
class StoredExecution:
    view: ExecutionView
    tenant_id: str
    subject_id: str
    source_uri: str
    backup_path: str


@dataclass(frozen=True, slots=True)
class PendingEvent:
    event_id: str
    execution_id: str
    payload: dict[str, Any]
    attempts: int


@dataclass(frozen=True, slots=True)
class WriteJournal:
    execution_id: str
    tenant_id: str
    subject_id: str
    document_id: str
    source_uri: str
    before_sha256: str
    after_sha256: str
    backup_path: str
    file_version_id: str
    event_id: str
    payload: dict[str, Any]
    state: str


SCHEMA_VERSION = 5


class StateStore:
    def __init__(self, database_path: Path) -> None:
        self._path = database_path.resolve()

    @property
    def path(self) -> Path:
        return self._path

    def healthcheck(self) -> bool:
        if not self._path.is_file():
            return False
        try:
            with self._connect() as connection:
                return bool(connection.execute("SELECT 1").fetchone()[0] == 1)
        except sqlite3.Error:
            return False

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(executions)").fetchall()
            }
            if "error_retryable" not in columns:
                connection.execute(
                    "ALTER TABLE executions ADD COLUMN error_retryable INTEGER NOT NULL DEFAULT 0"
                )
                connection.execute(
                    "UPDATE executions SET error_retryable = 1 WHERE sync_status = 'failed'"
                )
            agent_run_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(agent_runs)").fetchall()
            }
            if "runtime_fingerprint" not in agent_run_columns:
                connection.execute("ALTER TABLE agent_runs ADD COLUMN runtime_fingerprint TEXT")
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_runs_session_resume
                ON agent_runs(conversation_key, runtime_fingerprint, updated_at)
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, _now_text()),
            )
            connection.commit()

    def record_message(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        subject_id: str,
        message_id: str,
        role: str,
        content: str,
        response_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        conversation_key = _conversation_key(tenant_id, subject_id, conversation_id)
        now = _now_text()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO conversations(
                    conversation_key, external_id, tenant_id, subject_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (conversation_key, conversation_id, tenant_id, subject_id, now, now),
            )
            connection.execute(
                """
                INSERT INTO messages(
                    id, conversation_key, role, content, response_type, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    conversation_key,
                    role,
                    content,
                    response_type,
                    _json(metadata or {}),
                    now,
                ),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE conversation_key = ?",
                (now, conversation_key),
            )

    def record_agent_run(
        self,
        *,
        run_id: str,
        conversation_id: str,
        tenant_id: str,
        subject_id: str,
        status: str,
        provider: str,
        runtime_fingerprint: str | None = None,
        session_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        conversation_key = _conversation_key(tenant_id, subject_id, conversation_id)
        now = _now_text()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO agent_runs(
                    id, conversation_key, status, provider, runtime_fingerprint, session_id,
                    error_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    runtime_fingerprint = excluded.runtime_fingerprint,
                    session_id = excluded.session_id,
                    error_code = excluded.error_code,
                    updated_at = excluded.updated_at
                """,
                (
                    run_id,
                    conversation_key,
                    status,
                    provider,
                    runtime_fingerprint,
                    session_id,
                    error_code,
                    now,
                    now,
                ),
            )

    def latest_agent_session(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        subject_id: str,
        runtime_fingerprint: str,
        max_age_seconds: int,
    ) -> str | None:
        conversation_key = _conversation_key(tenant_id, subject_id, conversation_id)
        cutoff = datetime.now(UTC) - timedelta(seconds=max_age_seconds)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT status, session_id, updated_at FROM agent_runs
                WHERE conversation_key = ?
                  AND runtime_fingerprint = ?
                ORDER BY updated_at DESC, rowid DESC LIMIT 1
                """,
                (conversation_key, runtime_fingerprint),
            ).fetchone()
        if (
            row is None
            or row["status"] != "succeeded"
            or row["session_id"] is None
            or _parse_datetime(str(row["updated_at"])) < cutoff
        ):
            return None
        return str(row["session_id"])

    def get_chat_idempotency(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        key: str,
        request_hash: str,
    ) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT request_hash, response_json FROM chat_idempotency
                WHERE tenant_id = ? AND subject_id = ? AND key = ?
                """,
                (tenant_id, subject_id, key),
            ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise StateConflict(
                "IDEMPOTENCY_KEY_REUSED",
                "같은 Idempotency-Key를 다른 채팅 요청에 다시 사용할 수 없습니다.",
            )
        return str(row["response_json"])

    def put_chat_idempotency(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        key: str,
        request_hash: str,
        response_json: str,
    ) -> None:
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT request_hash FROM chat_idempotency
                WHERE tenant_id = ? AND subject_id = ? AND key = ?
                """,
                (tenant_id, subject_id, key),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise StateConflict(
                        "IDEMPOTENCY_KEY_REUSED",
                        "같은 Idempotency-Key를 다른 채팅 요청에 다시 사용할 수 없습니다.",
                    )
                return
            connection.execute(
                """
                INSERT INTO chat_idempotency(
                    tenant_id, subject_id, key, request_hash, response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (tenant_id, subject_id, key, request_hash, response_json, _now_text()),
            )

    def create_plan(
        self,
        plan: ChangePlanView,
        *,
        tenant_id: str,
        subject_id: str,
        request_text: str,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO change_plans(
                    id, tenant_id, subject_id, document_id, file_version_id, source_uri,
                    operation_json, unified_diff, base_sha256, proposed_sha256, plan_hash,
                    status, request_text, created_at, expires_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.change_plan_id,
                    tenant_id,
                    subject_id,
                    plan.document_id,
                    plan.file_version_id,
                    plan.source_uri,
                    _json(plan.operation.model_dump(mode="json")),
                    plan.unified_diff,
                    plan.base_sha256,
                    plan.proposed_sha256,
                    plan.plan_hash,
                    plan.status.value,
                    request_text,
                    plan.created_at.isoformat(),
                    plan.expires_at.isoformat(),
                    plan.created_at.isoformat(),
                ),
            )
            self._insert_audit(
                connection,
                tenant_id=tenant_id,
                subject_id=subject_id,
                action="change_plan.created",
                entity_type="change_plan",
                entity_id=plan.change_plan_id,
                metadata={"document_id": plan.document_id, "plan_hash": plan.plan_hash},
            )

    def get_plan(self, plan_id: str, *, tenant_id: str, subject_id: str) -> StoredPlan:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM change_plans
                WHERE id = ? AND tenant_id = ? AND subject_id = ?
                """,
                (plan_id, tenant_id, subject_id),
            ).fetchone()
        if row is None:
            raise StateNotFound(plan_id)
        return _stored_plan(row)

    def idempotent_execution(
        self,
        *,
        scope: str,
        tenant_id: str,
        subject_id: str,
        key: str,
        request_hash: str,
    ) -> ExecutionView | None:
        with self._connect() as connection:
            resource_id = self._idempotency_resource(
                connection,
                scope=scope,
                tenant_id=tenant_id,
                subject_id=subject_id,
                key=key,
                request_hash=request_hash,
            )
            if resource_id is None:
                return None
            row = connection.execute(
                "SELECT * FROM executions WHERE id = ? AND tenant_id = ? AND subject_id = ?",
                (resource_id, tenant_id, subject_id),
            ).fetchone()
        if row is None:
            raise StateConflict("IDEMPOTENCY_CORRUPT", "멱등성 기록이 손상되었습니다.")
        return _execution_view(row)

    def reject_plan(
        self,
        *,
        plan_id: str,
        tenant_id: str,
        subject_id: str,
        plan_hash: str,
        reason: str | None,
        idempotency_key: str,
        request_hash: str,
        approval_id: str,
    ) -> ChangePlanStatus:
        scope = f"POST:/api/v1/change-plans/{plan_id}/reject"
        with self._transaction() as connection:
            replay = self._idempotency_resource(
                connection,
                scope=scope,
                tenant_id=tenant_id,
                subject_id=subject_id,
                key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return ChangePlanStatus.REJECTED
            row = connection.execute(
                "SELECT * FROM change_plans WHERE id = ? AND tenant_id = ? AND subject_id = ?",
                (plan_id, tenant_id, subject_id),
            ).fetchone()
            if row is None:
                raise StateNotFound(plan_id)
            if row["plan_hash"] != plan_hash:
                raise StateConflict("PLAN_HASH_MISMATCH", "승인 화면의 plan_hash와 다릅니다.")
            if row["status"] != ChangePlanStatus.PENDING_APPROVAL.value:
                raise StateConflict("PLAN_NOT_PENDING", "이미 처리된 변경안입니다.")
            now = _now_text()
            updated = connection.execute(
                """
                UPDATE change_plans SET status = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    ChangePlanStatus.REJECTED.value,
                    now,
                    plan_id,
                    ChangePlanStatus.PENDING_APPROVAL.value,
                ),
            )
            if updated.rowcount != 1:
                raise StateConflict("PLAN_NOT_PENDING", "이미 처리된 변경안입니다.")
            connection.execute(
                """
                INSERT INTO approvals(
                    id, plan_id, actor_id, decision, plan_hash, reason, created_at
                )
                VALUES (?, ?, ?, 'rejected', ?, ?, ?)
                """,
                (approval_id, plan_id, subject_id, plan_hash, reason, now),
            )
            self._put_idempotency(
                connection,
                scope=scope,
                tenant_id=tenant_id,
                subject_id=subject_id,
                key=idempotency_key,
                request_hash=request_hash,
                resource_type="change_plan",
                resource_id=plan_id,
            )
            self._insert_audit(
                connection,
                tenant_id=tenant_id,
                subject_id=subject_id,
                action="change_plan.rejected",
                entity_type="change_plan",
                entity_id=plan_id,
                metadata={"reason": reason},
            )
        return ChangePlanStatus.REJECTED

    def prepare_execution(
        self,
        *,
        plan: StoredPlan,
        execution: ExecutionView,
        backup_path: str,
        event_id: str,
        file_version_id: str,
        event_payload: dict[str, Any],
        approval_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> ExecutionView:
        scope = f"POST:/api/v1/change-plans/{plan.view.change_plan_id}/approve"
        with self._transaction() as connection:
            replay = self._idempotency_resource(
                connection,
                scope=scope,
                tenant_id=plan.tenant_id,
                subject_id=plan.subject_id,
                key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                row = connection.execute(
                    "SELECT * FROM executions WHERE id = ?",
                    (replay,),
                ).fetchone()
                if row is None:
                    raise StateConflict("IDEMPOTENCY_CORRUPT", "멱등성 기록이 손상되었습니다.")
                return _execution_view(row)

            row = connection.execute(
                """
                SELECT status, plan_hash, expires_at FROM change_plans
                WHERE id = ? AND tenant_id = ? AND subject_id = ?
                """,
                (plan.view.change_plan_id, plan.tenant_id, plan.subject_id),
            ).fetchone()
            if row is None:
                raise StateNotFound(plan.view.change_plan_id)
            if row["status"] != ChangePlanStatus.PENDING_APPROVAL.value:
                raise StateConflict("PLAN_NOT_PENDING", "이미 처리된 변경안입니다.")
            if _parse_datetime(row["expires_at"]) <= datetime.now(UTC):
                connection.execute(
                    "UPDATE change_plans SET status = ?, updated_at = ? WHERE id = ?",
                    (ChangePlanStatus.EXPIRED.value, _now_text(), plan.view.change_plan_id),
                )
                raise StateConflict("PLAN_EXPIRED", "변경안이 만료되었습니다.")

            now = execution.created_at.isoformat()
            updated = connection.execute(
                """
                UPDATE change_plans SET status = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    ChangePlanStatus.CONSUMED.value,
                    now,
                    plan.view.change_plan_id,
                    ChangePlanStatus.PENDING_APPROVAL.value,
                ),
            )
            if updated.rowcount != 1:
                raise StateConflict("PLAN_NOT_PENDING", "이미 처리된 변경안입니다.")
            connection.execute(
                """
                INSERT INTO approvals(
                    id, plan_id, actor_id, decision, plan_hash, reason, created_at
                )
                VALUES (?, ?, ?, 'approved', ?, NULL, ?)
                """,
                (approval_id, plan.view.change_plan_id, plan.subject_id, plan.view.plan_hash, now),
            )
            self._insert_execution(
                connection, execution, plan.tenant_id, plan.subject_id, backup_path
            )
            connection.execute(
                """
                INSERT INTO write_journals(
                    execution_id, tenant_id, subject_id, document_id, source_uri,
                    before_sha256, after_sha256, backup_path, file_version_id,
                    event_id, event_payload_json, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)
                """,
                (
                    execution.execution_id,
                    plan.tenant_id,
                    plan.subject_id,
                    execution.document_id,
                    plan.view.source_uri,
                    execution.before_sha256,
                    execution.after_sha256,
                    backup_path,
                    file_version_id,
                    event_id,
                    _json(event_payload),
                    now,
                    now,
                ),
            )
            self._put_idempotency(
                connection,
                scope=scope,
                tenant_id=plan.tenant_id,
                subject_id=plan.subject_id,
                key=idempotency_key,
                request_hash=request_hash,
                resource_type="execution",
                resource_id=execution.execution_id,
            )
            self._insert_audit(
                connection,
                tenant_id=plan.tenant_id,
                subject_id=plan.subject_id,
                action="change_plan.approved",
                entity_type="execution",
                entity_id=execution.execution_id,
                metadata={"change_plan_id": plan.view.change_plan_id},
            )
        return execution

    def prepare_undo(
        self,
        *,
        original: StoredExecution,
        execution: ExecutionView,
        backup_path: str,
        event_id: str,
        file_version_id: str,
        event_payload: dict[str, Any],
        idempotency_key: str,
        request_hash: str,
    ) -> ExecutionView:
        scope = f"POST:/api/v1/executions/{original.view.execution_id}/undo"
        with self._transaction() as connection:
            replay = self._idempotency_resource(
                connection,
                scope=scope,
                tenant_id=original.tenant_id,
                subject_id=original.subject_id,
                key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                row = connection.execute(
                    "SELECT * FROM executions WHERE id = ?",
                    (replay,),
                ).fetchone()
                if row is None:
                    raise StateConflict("IDEMPOTENCY_CORRUPT", "멱등성 기록이 손상되었습니다.")
                return _execution_view(row)
            current = connection.execute(
                "SELECT * FROM executions WHERE id = ? AND tenant_id = ? AND subject_id = ?",
                (original.view.execution_id, original.tenant_id, original.subject_id),
            ).fetchone()
            if current is None:
                raise StateNotFound(original.view.execution_id)
            if current["status"] not in {
                ExecutionStatus.COMPLETED.value,
                ExecutionStatus.FILE_APPLIED.value,
            }:
                raise StateConflict("UNDO_NOT_AVAILABLE", "되돌릴 수 있는 완료 실행이 아닙니다.")
            if connection.execute(
                "SELECT 1 FROM executions WHERE undo_of_execution_id = ?",
                (original.view.execution_id,),
            ).fetchone():
                raise StateConflict("UNDO_ALREADY_USED", "이미 되돌린 실행입니다.")
            self._insert_execution(
                connection,
                execution,
                original.tenant_id,
                original.subject_id,
                backup_path,
            )
            now = execution.created_at.isoformat()
            connection.execute(
                """
                INSERT INTO write_journals(
                    execution_id, tenant_id, subject_id, document_id, source_uri,
                    before_sha256, after_sha256, backup_path, file_version_id,
                    event_id, event_payload_json, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)
                """,
                (
                    execution.execution_id,
                    original.tenant_id,
                    original.subject_id,
                    execution.document_id,
                    original.source_uri,
                    execution.before_sha256,
                    execution.after_sha256,
                    backup_path,
                    file_version_id,
                    event_id,
                    _json(event_payload),
                    now,
                    now,
                ),
            )
            self._put_idempotency(
                connection,
                scope=scope,
                tenant_id=original.tenant_id,
                subject_id=original.subject_id,
                key=idempotency_key,
                request_hash=request_hash,
                resource_type="execution",
                resource_id=execution.execution_id,
            )
            self._insert_audit(
                connection,
                tenant_id=original.tenant_id,
                subject_id=original.subject_id,
                action="execution.undo_prepared",
                entity_type="execution",
                entity_id=execution.execution_id,
                metadata={"undo_of_execution_id": original.view.execution_id},
            )
        return execution

    def finalize_file_write(self, execution_id: str) -> None:
        with self._transaction() as connection:
            journal = connection.execute(
                "SELECT * FROM write_journals WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if journal is None:
                raise StateNotFound(execution_id)
            if journal["state"] == "finalized":
                return
            if journal["state"] not in {"prepared", "replaced"}:
                raise StateConflict(
                    "JOURNAL_NOT_RECOVERABLE", "쓰기 저널 상태가 유효하지 않습니다."
                )
            now = _now_text()
            connection.execute(
                """
                UPDATE write_journals SET state = 'finalized', updated_at = ?
                WHERE execution_id = ?
                """,
                (now, execution_id),
            )
            connection.execute(
                """
                UPDATE executions SET status = ?, file_status = ?, sync_status = ?,
                    file_version_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    ExecutionStatus.FILE_APPLIED.value,
                    FileStatus.APPLIED.value,
                    SyncStatus.PENDING.value,
                    journal["file_version_id"],
                    now,
                    execution_id,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO file_versions(
                    id, execution_id, document_id, source_uri, sha256, backup_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    journal["file_version_id"],
                    execution_id,
                    journal["document_id"],
                    journal["source_uri"],
                    journal["after_sha256"],
                    journal["backup_path"],
                    now,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO outbox_events(
                    id, event_type, aggregate_id, payload_json, status, attempts,
                    created_at, updated_at
                ) VALUES (?, 'DocumentContentChanged', ?, ?, 'pending', 0, ?, ?)
                """,
                (
                    journal["event_id"],
                    execution_id,
                    journal["event_payload_json"],
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE outbox_events SET status = 'processed', processed_at = ?,
                    updated_at = ?, last_error = 'compensated by undo'
                WHERE status = 'failed' AND aggregate_id = (
                    SELECT undo_of_execution_id FROM executions WHERE id = ?
                )
                """,
                (now, now, execution_id),
            )
            self._insert_audit(
                connection,
                tenant_id=journal["tenant_id"],
                subject_id=journal["subject_id"],
                action="file.applied",
                entity_type="execution",
                entity_id=execution_id,
                metadata={
                    "document_id": journal["document_id"],
                    "after_sha256": journal["after_sha256"],
                },
            )

    def mark_journal_replaced(self, execution_id: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE write_journals SET state = 'replaced', updated_at = ?
                WHERE execution_id = ? AND state = 'prepared'
                """,
                (_now_text(), execution_id),
            )

    def unfinished_journals(self) -> list[WriteJournal]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM write_journals
                WHERE state IN ('prepared', 'replaced') ORDER BY created_at, execution_id
                """
            ).fetchall()
        return [
            WriteJournal(
                execution_id=row["execution_id"],
                tenant_id=row["tenant_id"],
                subject_id=row["subject_id"],
                document_id=row["document_id"],
                source_uri=row["source_uri"],
                before_sha256=row["before_sha256"],
                after_sha256=row["after_sha256"],
                backup_path=row["backup_path"],
                file_version_id=row["file_version_id"],
                event_id=row["event_id"],
                payload=json.loads(row["event_payload_json"]),
                state=row["state"],
            )
            for row in rows
        ]

    def get_write_journal(
        self,
        execution_id: str,
        *,
        tenant_id: str,
        subject_id: str,
    ) -> WriteJournal:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM write_journals
                WHERE execution_id = ? AND tenant_id = ? AND subject_id = ?
                """,
                (execution_id, tenant_id, subject_id),
            ).fetchone()
        if row is None:
            raise StateNotFound(execution_id)
        return WriteJournal(
            execution_id=row["execution_id"],
            tenant_id=row["tenant_id"],
            subject_id=row["subject_id"],
            document_id=row["document_id"],
            source_uri=row["source_uri"],
            before_sha256=row["before_sha256"],
            after_sha256=row["after_sha256"],
            backup_path=row["backup_path"],
            file_version_id=row["file_version_id"],
            event_id=row["event_id"],
            payload=json.loads(row["event_payload_json"]),
            state=row["state"],
        )

    def execution_for_plan(
        self,
        plan_id: str,
        *,
        tenant_id: str,
        subject_id: str,
    ) -> StoredExecution | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM executions
                WHERE change_plan_id = ? AND tenant_id = ? AND subject_id = ?
                """,
                (plan_id, tenant_id, subject_id),
            ).fetchone()
        return _stored_execution(row) if row is not None else None

    def undo_execution_for(
        self,
        original_execution_id: str,
        *,
        tenant_id: str,
        subject_id: str,
    ) -> StoredExecution | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM executions
                WHERE undo_of_execution_id = ? AND tenant_id = ? AND subject_id = ?
                """,
                (original_execution_id, tenant_id, subject_id),
            ).fetchone()
        return _stored_execution(row) if row is not None else None

    def rearm_aborted_write(
        self,
        execution_id: str,
        *,
        tenant_id: str,
        subject_id: str,
        idempotency_scope: str,
        idempotency_key: str,
        request_hash: str,
    ) -> ExecutionView:
        """Atomically bind a retry key and make a no-effect failed write runnable again."""

        with self._transaction() as connection:
            resource_id = self._idempotency_resource(
                connection,
                scope=idempotency_scope,
                tenant_id=tenant_id,
                subject_id=subject_id,
                key=idempotency_key,
                request_hash=request_hash,
            )
            if resource_id is not None and resource_id != execution_id:
                raise StateConflict(
                    "IDEMPOTENCY_KEY_REUSED",
                    "같은 Idempotency-Key가 다른 실행에 연결되어 있습니다.",
                )
            row = connection.execute(
                """
                SELECT executions.*, write_journals.state AS journal_state
                FROM executions
                JOIN write_journals ON write_journals.execution_id = executions.id
                WHERE executions.id = ?
                  AND executions.tenant_id = ?
                  AND executions.subject_id = ?
                """,
                (execution_id, tenant_id, subject_id),
            ).fetchone()
            if row is None:
                raise StateNotFound(execution_id)
            if not (
                row["status"] == ExecutionStatus.FAILED.value
                and row["file_status"] == FileStatus.FAILED.value
                and row["journal_state"] == "aborted"
            ):
                raise StateConflict(
                    "WRITE_RETRY_NOT_AVAILABLE",
                    "원본에 반영되지 않은 실패한 파일 쓰기만 다시 시도할 수 있습니다.",
                )
            now = _now_text()
            connection.execute(
                """
                UPDATE write_journals SET state = 'prepared', updated_at = ?
                WHERE execution_id = ? AND state = 'aborted'
                """,
                (now, execution_id),
            )
            connection.execute(
                """
                UPDATE executions SET status = ?, file_status = ?, sync_status = ?,
                    error_code = NULL, error_message = NULL, error_retryable = 0, updated_at = ?
                WHERE id = ?
                """,
                (
                    ExecutionStatus.PREPARED.value,
                    FileStatus.PENDING.value,
                    SyncStatus.PENDING.value,
                    now,
                    execution_id,
                ),
            )
            if resource_id is None:
                self._put_idempotency(
                    connection,
                    scope=idempotency_scope,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    key=idempotency_key,
                    request_hash=request_hash,
                    resource_type="execution",
                    resource_id=execution_id,
                )
            self._insert_audit(
                connection,
                tenant_id=tenant_id,
                subject_id=subject_id,
                action="file.retry_prepared",
                entity_type="execution",
                entity_id=execution_id,
                metadata={},
            )
            refreshed = connection.execute(
                "SELECT * FROM executions WHERE id = ?",
                (execution_id,),
            ).fetchone()
        assert refreshed is not None
        return _execution_view(refreshed)

    def abort_prepared_write(self, execution_id: str, *, code: str, message: str) -> None:
        with self._transaction() as connection:
            now = _now_text()
            connection.execute(
                """
                UPDATE write_journals SET state = 'aborted', updated_at = ?
                WHERE execution_id = ?
                """,
                (now, execution_id),
            )
            connection.execute(
                """
                UPDATE executions SET status = ?, file_status = ?, sync_status = ?,
                    error_code = ?, error_message = ?, error_retryable = 1,
                    updated_at = ? WHERE id = ?
                """,
                (
                    ExecutionStatus.FAILED.value,
                    FileStatus.FAILED.value,
                    SyncStatus.FAILED.value,
                    code,
                    message,
                    now,
                    execution_id,
                ),
            )

    def mark_execution_conflict(self, execution_id: str, *, code: str, message: str) -> None:
        with self._transaction() as connection:
            now = _now_text()
            connection.execute(
                """
                UPDATE executions SET status = ?, file_status = ?, sync_status = ?,
                    error_code = ?, error_message = ?, error_retryable = 0,
                    updated_at = ? WHERE id = ?
                """,
                (
                    ExecutionStatus.CONFLICT.value,
                    FileStatus.CONFLICT.value,
                    SyncStatus.FAILED.value,
                    code,
                    message,
                    now,
                    execution_id,
                ),
            )
            connection.execute(
                """
                UPDATE write_journals SET state = 'conflict', updated_at = ?
                WHERE execution_id = ?
                """,
                (now, execution_id),
            )

    def get_execution(
        self,
        execution_id: str,
        *,
        tenant_id: str,
        subject_id: str,
    ) -> StoredExecution:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM executions
                WHERE id = ? AND tenant_id = ? AND subject_id = ?
                """,
                (execution_id, tenant_id, subject_id),
            ).fetchone()
        if row is None:
            raise StateNotFound(execution_id)
        return _stored_execution(row)

    def pending_events(self) -> list[PendingEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, aggregate_id, payload_json, attempts FROM outbox_events
                WHERE status IN ('pending', 'processing')
                ORDER BY created_at, id
                """
            ).fetchall()
        return [
            PendingEvent(
                event_id=row["id"],
                execution_id=row["aggregate_id"],
                payload=json.loads(row["payload_json"]),
                attempts=row["attempts"],
            )
            for row in rows
        ]

    def has_unpublished_events(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM outbox_events
                WHERE status IN ('pending', 'processing', 'failed') LIMIT 1
                """
            ).fetchone()
        return row is not None

    def has_failed_sync_events(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM outbox_events WHERE status = 'failed' LIMIT 1"
            ).fetchone()
        return row is not None

    def sync_event_counts(self) -> tuple[int, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count FROM outbox_events
                WHERE status IN ('pending', 'processing', 'failed')
                GROUP BY status
                """
            ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return counts.get("pending", 0) + counts.get("processing", 0), counts.get("failed", 0)

    def stale_sync_event_count(self, *, stale_after_seconds: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(seconds=stale_after_seconds)).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) FROM outbox_events
                WHERE status IN ('pending', 'processing') AND updated_at <= ?
                """,
                (cutoff,),
            ).fetchone()
        assert row is not None
        return int(row[0])

    def begin_event_batch(self, event_ids: list[str]) -> None:
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        with self._transaction() as connection:
            now = _now_text()
            connection.execute(
                f"""
                UPDATE outbox_events SET status = 'processing', attempts = attempts + 1,
                    updated_at = ? WHERE id IN ({placeholders})
                """,
                (now, *event_ids),
            )
            connection.execute(
                f"""
                UPDATE executions SET status = ?, sync_status = ?, updated_at = ?
                WHERE id IN (
                    SELECT aggregate_id FROM outbox_events WHERE id IN ({placeholders})
                )
                """,
                (ExecutionStatus.SYNCING.value, SyncStatus.RUNNING.value, now, *event_ids),
            )

    def record_sync_task(
        self,
        event_ids: list[str],
        *,
        stage: str,
        status: str,
        output: dict[str, Any] | None = None,
    ) -> None:
        if not event_ids:
            return
        with self._transaction() as connection:
            now = _now_text()
            for event_id in event_ids:
                connection.execute(
                    """
                    INSERT INTO sync_tasks(event_id, stage, status, output_json, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(event_id, stage) DO UPDATE SET
                        status = excluded.status,
                        output_json = excluded.output_json,
                        updated_at = excluded.updated_at
                    """,
                    (event_id, stage, status, _json(output or {}), now),
                )

    def record_llmwiki_input_state(
        self,
        *,
        document_id: str,
        active_build_id: str,
        source_sha256: str,
        revision: str,
        content_sha256: str,
        event_ids: list[str],
    ) -> None:
        """Trust one normalized input only after deriving it from approved events."""
        with self._transaction() as connection:
            connection.execute(
                """
                DELETE FROM llmwiki_input_states
                WHERE document_id = ? AND active_build_id != ?
                """,
                (document_id, active_build_id),
            )
            connection.execute(
                """
                INSERT INTO llmwiki_input_states(
                    document_id, active_build_id, source_sha256, revision,
                    content_sha256, event_ids_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id, active_build_id, content_sha256) DO UPDATE SET
                    source_sha256 = excluded.source_sha256,
                    revision = excluded.revision,
                    event_ids_json = excluded.event_ids_json
                """,
                (
                    document_id,
                    active_build_id,
                    source_sha256,
                    revision,
                    content_sha256,
                    _json(event_ids),
                    _now_text(),
                ),
            )

    def is_known_llmwiki_input_state(
        self,
        *,
        document_id: str,
        active_build_id: str,
        content_sha256: str,
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM llmwiki_input_states
                WHERE document_id = ? AND active_build_id = ? AND content_sha256 = ?
                LIMIT 1
                """,
                (document_id, active_build_id, content_sha256),
            ).fetchone()
        return row is not None

    def record_llmwiki_build_digest(
        self,
        *,
        build_id: str,
        native_digest: str,
        parent_build_id: str | None,
        status: str,
    ) -> None:
        if status not in {"validated", "active"}:
            raise ValueError("invalid LLMWIKI build digest status")
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT native_digest, status FROM llmwiki_build_digests
                WHERE build_id = ?
                """,
                (build_id,),
            ).fetchone()
            if existing is not None and existing["native_digest"] != native_digest:
                raise StateConflict(
                    "LLMWIKI_BUILD_DIGEST_CONFLICT",
                    "검증된 LLMWIKI 빌드 내용이 변경되었습니다.",
                )
            effective_status = (
                "active"
                if status == "active" or (existing is not None and existing["status"] == "active")
                else "validated"
            )
            now = _now_text()
            connection.execute(
                """
                INSERT INTO llmwiki_build_digests(
                    build_id, native_digest, parent_build_id, status, validated_at, activated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(build_id) DO UPDATE SET
                    parent_build_id = COALESCE(
                        llmwiki_build_digests.parent_build_id, excluded.parent_build_id
                    ),
                    status = excluded.status,
                    activated_at = COALESCE(
                        llmwiki_build_digests.activated_at, excluded.activated_at
                    )
                """,
                (
                    build_id,
                    native_digest,
                    parent_build_id,
                    effective_status,
                    now,
                    now if effective_status == "active" else None,
                ),
            )

    def llmwiki_build_digest(self, build_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT native_digest FROM llmwiki_build_digests
                WHERE build_id = ?
                """,
                (build_id,),
            ).fetchone()
        return str(row["native_digest"]) if row is not None else None

    def complete_event_batch(self, event_ids: list[str], graph_version: str) -> None:
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        with self._transaction() as connection:
            now = _now_text()
            connection.execute(
                f"""
                UPDATE outbox_events SET status = 'processed', processed_at = ?, updated_at = ?,
                    last_error = NULL WHERE id IN ({placeholders})
                """,
                (now, now, *event_ids),
            )
            connection.execute(
                f"""
                UPDATE executions SET status = ?, sync_status = ?, graph_version_after = ?,
                    error_code = NULL, error_message = NULL, error_retryable = 0, updated_at = ?
                WHERE id IN (
                    SELECT aggregate_id FROM outbox_events WHERE id IN ({placeholders})
                )
                """,
                (
                    ExecutionStatus.COMPLETED.value,
                    SyncStatus.PUBLISHED.value,
                    graph_version,
                    now,
                    *event_ids,
                ),
            )
            connection.execute(
                f"""
                UPDATE executions SET status = ?, updated_at = ?
                WHERE id IN (
                    SELECT undo_of_execution_id FROM executions
                    WHERE id IN (
                        SELECT aggregate_id FROM outbox_events WHERE id IN ({placeholders})
                    ) AND undo_of_execution_id IS NOT NULL
                )
                """,
                (ExecutionStatus.UNDONE.value, now, *event_ids),
            )
            for event_id in event_ids:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO processed_events(consumer, event_id, processed_at)
                    VALUES ('knowledge-publisher', ?, ?)
                    """,
                    (event_id, now),
                )

    def fail_event_batch(
        self,
        event_ids: list[str],
        *,
        code: str,
        message: str,
        retryable: bool = True,
    ) -> None:
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        with self._transaction() as connection:
            now = _now_text()
            connection.execute(
                f"""
                UPDATE outbox_events SET status = 'failed', last_error = ?, updated_at = ?
                WHERE id IN ({placeholders})
                """,
                (message, now, *event_ids),
            )
            connection.execute(
                f"""
                UPDATE executions SET status = ?, sync_status = ?, error_code = ?,
                    error_message = ?, error_retryable = ?, updated_at = ?
                WHERE id IN (
                    SELECT aggregate_id FROM outbox_events WHERE id IN ({placeholders})
                )
                """,
                (
                    ExecutionStatus.FILE_APPLIED.value,
                    SyncStatus.FAILED.value,
                    code,
                    message,
                    int(retryable),
                    now,
                    *event_ids,
                ),
            )

    def queue_failed_execution(self, execution_id: str) -> None:
        with self._transaction() as connection:
            now = _now_text()
            target = connection.execute(
                """
                SELECT 1 FROM outbox_events
                JOIN executions ON executions.id = outbox_events.aggregate_id
                WHERE outbox_events.aggregate_id = ?
                  AND outbox_events.status = 'failed'
                  AND executions.error_retryable = 1
                """,
                (execution_id,),
            ).fetchone()
            if target is None:
                raise StateConflict(
                    "SYNC_RETRY_NOT_AVAILABLE",
                    "재시도할 수 있는 실패 event를 찾을 수 없습니다.",
                )
            connection.execute(
                """
                UPDATE outbox_events SET status = 'pending', last_error = NULL, updated_at = ?
                WHERE status = 'failed' AND aggregate_id IN (
                    SELECT id FROM executions WHERE error_retryable = 1
                )
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE executions SET status = ?, sync_status = ?, error_code = NULL,
                    error_message = NULL, error_retryable = 0, updated_at = ?
                WHERE id IN (
                    SELECT aggregate_id FROM outbox_events WHERE status = 'pending'
                ) AND sync_status = ?
                """,
                (
                    ExecutionStatus.FILE_APPLIED.value,
                    SyncStatus.PENDING.value,
                    now,
                    SyncStatus.FAILED.value,
                ),
            )

    def record_knowledge_version(
        self,
        *,
        version: str,
        parent_version: str | None,
        event_ids: list[str],
        status: str,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO knowledge_versions(
                    version, parent_version, event_ids_json, status, created_at, activated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(version) DO UPDATE SET status = excluded.status,
                    activated_at = excluded.activated_at
                """,
                (
                    version,
                    parent_version,
                    _json(event_ids),
                    status,
                    _now_text(),
                    _now_text() if status == "active" else None,
                ),
            )

    def reset(self) -> None:
        tables = [
            "processed_events",
            "sync_tasks",
            "llmwiki_input_states",
            "llmwiki_build_digests",
            "outbox_events",
            "write_journals",
            "file_versions",
            "approvals",
            "idempotency_records",
            "chat_idempotency",
            "executions",
            "change_plans",
            "agent_runs",
            "messages",
            "conversations",
            "audit_events",
            "knowledge_versions",
        ]
        with self._transaction() as connection:
            for table in tables:
                connection.execute(f"DELETE FROM {table}")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _insert_execution(
        self,
        connection: sqlite3.Connection,
        execution: ExecutionView,
        tenant_id: str,
        subject_id: str,
        backup_path: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO executions(
                id, change_plan_id, undo_of_execution_id, tenant_id, subject_id,
                document_id, source_uri, status, file_status, sync_status,
                before_sha256, after_sha256, backup_path, file_version_id,
                graph_version_before, graph_version_after, error_code, error_message,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (
                execution.execution_id,
                execution.change_plan_id,
                execution.undo_of_execution_id,
                tenant_id,
                subject_id,
                execution.document_id,
                "",
                execution.status.value,
                execution.file_status.value,
                execution.sync_status.value,
                execution.before_sha256,
                execution.after_sha256,
                backup_path,
                execution.file_version_id,
                execution.graph_version_before,
                execution.graph_version_after,
                execution.created_at.isoformat(),
                execution.updated_at.isoformat(),
            ),
        )
        source_uri = connection.execute(
            "SELECT source_uri FROM change_plans WHERE id = ?",
            (execution.change_plan_id,),
        ).fetchone()
        if source_uri is not None:
            connection.execute(
                "UPDATE executions SET source_uri = ? WHERE id = ?",
                (source_uri["source_uri"], execution.execution_id),
            )
        elif execution.undo_of_execution_id:
            connection.execute(
                """
                UPDATE executions SET source_uri = (
                    SELECT source_uri FROM executions WHERE id = ?
                ) WHERE id = ?
                """,
                (execution.undo_of_execution_id, execution.execution_id),
            )

    def _idempotency_resource(
        self,
        connection: sqlite3.Connection,
        *,
        scope: str,
        tenant_id: str,
        subject_id: str,
        key: str,
        request_hash: str,
    ) -> str | None:
        row = connection.execute(
            """
            SELECT request_hash, resource_id FROM idempotency_records
            WHERE scope = ? AND tenant_id = ? AND subject_id = ? AND key = ?
            """,
            (scope, tenant_id, subject_id, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise StateConflict(
                "IDEMPOTENCY_KEY_REUSED",
                "같은 Idempotency-Key를 다른 요청 본문에 다시 사용할 수 없습니다.",
            )
        return str(row["resource_id"])

    def _put_idempotency(
        self,
        connection: sqlite3.Connection,
        *,
        scope: str,
        tenant_id: str,
        subject_id: str,
        key: str,
        request_hash: str,
        resource_type: str,
        resource_id: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO idempotency_records(
                scope, tenant_id, subject_id, key, request_hash,
                resource_type, resource_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope,
                tenant_id,
                subject_id,
                key,
                request_hash,
                resource_type,
                resource_id,
                _now_text(),
            ),
        )

    def _insert_audit(
        self,
        connection: sqlite3.Connection,
        *,
        tenant_id: str,
        subject_id: str,
        action: str,
        entity_type: str,
        entity_id: str,
        metadata: dict[str, Any],
    ) -> None:
        from uuid import uuid4

        connection.execute(
            """
            INSERT INTO audit_events(
                id, tenant_id, subject_id, action, entity_type,
                entity_id, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"audit_{uuid4().hex}",
                tenant_id,
                subject_id,
                action,
                entity_type,
                entity_id,
                _json(metadata),
                _now_text(),
            ),
        )


def _stored_plan(row: sqlite3.Row) -> StoredPlan:
    return StoredPlan(
        view=ChangePlanView(
            change_plan_id=row["id"],
            document_id=row["document_id"],
            file_version_id=row["file_version_id"],
            source_uri=row["source_uri"],
            operation=ReplaceExactOperation.model_validate(json.loads(row["operation_json"])),
            unified_diff=row["unified_diff"],
            base_sha256=row["base_sha256"],
            proposed_sha256=row["proposed_sha256"],
            plan_hash=row["plan_hash"],
            status=ChangePlanStatus(row["status"]),
            created_at=_parse_datetime(row["created_at"]),
            expires_at=_parse_datetime(row["expires_at"]),
        ),
        tenant_id=row["tenant_id"],
        subject_id=row["subject_id"],
        request_text=row["request_text"],
    )


def _stored_execution(row: sqlite3.Row) -> StoredExecution:
    return StoredExecution(
        view=_execution_view(row),
        tenant_id=row["tenant_id"],
        subject_id=row["subject_id"],
        source_uri=row["source_uri"],
        backup_path=row["backup_path"],
    )


def _execution_view(row: sqlite3.Row) -> ExecutionView:
    from codegate_api.models import ApiError

    error = None
    if row["error_code"]:
        error = ApiError(
            code=row["error_code"],
            message=row["error_message"] or "",
            retryable=bool(row["error_retryable"]),
        )
    return ExecutionView(
        execution_id=row["id"],
        change_plan_id=row["change_plan_id"],
        undo_of_execution_id=row["undo_of_execution_id"],
        document_id=row["document_id"],
        status=ExecutionStatus(row["status"]),
        file_status=FileStatus(row["file_status"]),
        sync_status=SyncStatus(row["sync_status"]),
        before_sha256=row["before_sha256"],
        after_sha256=row["after_sha256"],
        file_version_id=row["file_version_id"],
        graph_version_before=row["graph_version_before"],
        graph_version_after=row["graph_version_after"],
        error=error,
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _conversation_key(tenant_id: str, subject_id: str, conversation_id: str) -> str:
    import hashlib

    return hashlib.sha256(
        f"codegate-conversation-v1\x00{tenant_id}\x00{subject_id}\x00{conversation_id}".encode()
    ).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now_text() -> str:
    return datetime.now(UTC).isoformat()


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    conversation_key TEXT PRIMARY KEY,
    external_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_key TEXT NOT NULL REFERENCES conversations(conversation_key),
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    response_type TEXT,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    conversation_key TEXT NOT NULL REFERENCES conversations(conversation_key),
    status TEXT NOT NULL,
    provider TEXT NOT NULL,
    runtime_fingerprint TEXT,
    session_id TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_plans (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    file_version_id TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    operation_json TEXT NOT NULL,
    unified_diff TEXT NOT NULL,
    base_sha256 TEXT NOT NULL CHECK(length(base_sha256) = 64),
    proposed_sha256 TEXT NOT NULL CHECK(length(proposed_sha256) = 64),
    plan_hash TEXT NOT NULL UNIQUE CHECK(length(plan_hash) = 64),
    status TEXT NOT NULL CHECK(status IN (
        'pending_approval', 'approved', 'rejected', 'expired', 'consumed'
    )),
    request_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES change_plans(id),
    actor_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approved', 'rejected')),
    plan_hash TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(plan_id)
);

CREATE TABLE IF NOT EXISTS executions (
    id TEXT PRIMARY KEY,
    change_plan_id TEXT REFERENCES change_plans(id),
    undo_of_execution_id TEXT REFERENCES executions(id),
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    status TEXT NOT NULL,
    file_status TEXT NOT NULL,
    sync_status TEXT NOT NULL,
    before_sha256 TEXT NOT NULL CHECK(length(before_sha256) = 64),
    after_sha256 TEXT NOT NULL CHECK(length(after_sha256) = 64),
    backup_path TEXT NOT NULL,
    file_version_id TEXT,
    graph_version_before TEXT NOT NULL,
    graph_version_after TEXT,
    error_code TEXT,
    error_message TEXT,
    error_retryable INTEGER NOT NULL DEFAULT 0 CHECK(error_retryable IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(change_plan_id),
    UNIQUE(undo_of_execution_id)
);

CREATE TABLE IF NOT EXISTS write_journals (
    execution_id TEXT PRIMARY KEY REFERENCES executions(id),
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    before_sha256 TEXT NOT NULL,
    after_sha256 TEXT NOT NULL,
    backup_path TEXT NOT NULL,
    file_version_id TEXT NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    event_payload_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'prepared', 'replaced', 'finalized', 'aborted', 'conflict'
    )),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS file_versions (
    id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL UNIQUE REFERENCES executions(id),
    document_id TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
    backup_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_records (
    scope TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    key TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK(length(request_hash) = 64),
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, tenant_id, subject_id, key)
);

CREATE TABLE IF NOT EXISTS chat_idempotency (
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    key TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK(length(request_hash) = 64),
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(tenant_id, subject_id, key)
);

CREATE TABLE IF NOT EXISTS outbox_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL REFERENCES executions(id),
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'processing', 'processed', 'failed')),
    attempts INTEGER NOT NULL CHECK(attempts >= 0),
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS processed_events (
    consumer TEXT NOT NULL,
    event_id TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY(consumer, event_id)
);

CREATE TABLE IF NOT EXISTS sync_tasks (
    event_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    output_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(event_id, stage)
);

CREATE TABLE IF NOT EXISTS llmwiki_input_states (
    document_id TEXT NOT NULL,
    active_build_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    revision TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
    event_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(document_id, active_build_id, content_sha256)
);

CREATE TABLE IF NOT EXISTS llmwiki_build_digests (
    build_id TEXT PRIMARY KEY,
    native_digest TEXT NOT NULL CHECK(length(native_digest) = 64),
    parent_build_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('validated', 'active')),
    validated_at TEXT NOT NULL,
    activated_at TEXT
);

CREATE TABLE IF NOT EXISTS knowledge_versions (
    version TEXT PRIMARY KEY,
    parent_version TEXT,
    event_ids_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    activated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_status_created
ON outbox_events(status, created_at);
CREATE INDEX IF NOT EXISTS idx_executions_owner
ON executions(tenant_id, subject_id, created_at);
"""
