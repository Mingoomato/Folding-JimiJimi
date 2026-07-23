from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class DocumentStateError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class StoredDocumentPlan:
    values: dict[str, Any]
    artifacts: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class StoredDocumentExecution:
    values: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PendingDocumentEvent:
    event_id: str
    execution_id: str
    payload: dict[str, Any]
    attempts: int


class DocumentStateStore:
    def __init__(self, database_path: Path) -> None:
        self._path = database_path.resolve()

    def create_preparing_plan(
        self,
        *,
        plan_id: str,
        tenant_id: str,
        subject_id: str,
        kind: str,
        document_id: str,
        format_: str,
        capability_id: str,
        source_uri: str | None,
        target_relative_path: str | None,
        request: dict[str, Any],
        operations: list[dict[str, Any]],
        graph_version: str,
        capability_snapshot_id: str,
        created_at: datetime,
        expires_at: datetime,
        idempotency_key: str,
        request_hash: str,
    ) -> str:
        with self._transaction() as connection:
            existing = self._idempotent_resource(
                connection,
                scope="document-plan-create",
                tenant_id=tenant_id,
                subject_id=subject_id,
                key=idempotency_key,
                request_hash=request_hash,
            )
            if existing:
                return existing
            try:
                connection.execute(
                    """
                    INSERT INTO document_plans_v2(
                        id, tenant_id, subject_id, kind, document_id, format,
                        capability_id, source_uri, target_relative_path, request_json,
                        operations_json, status, graph_version, capability_snapshot_id,
                        structural_diff_json, warnings_json, created_at, expires_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'preparing', ?, ?, '[]', '[]', ?, ?, ?
                    )
                    """,
                    (
                        plan_id,
                        tenant_id,
                        subject_id,
                        kind,
                        document_id,
                        format_,
                        capability_id,
                        source_uri,
                        target_relative_path,
                        _json(request),
                        _json(operations),
                        graph_version,
                        capability_snapshot_id,
                        created_at.isoformat(),
                        expires_at.isoformat(),
                        created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                if target_relative_path:
                    raise DocumentStateError(
                        "target_exists",
                        "an active plan already reserves this creation target",
                    ) from error
                raise
            self._insert_idempotency(
                connection,
                scope="document-plan-create",
                tenant_id=tenant_id,
                subject_id=subject_id,
                key=idempotency_key,
                request_hash=request_hash,
                resource_type="document_plan",
                resource_id=plan_id,
            )
            return plan_id

    def complete_plan(
        self,
        *,
        plan_id: str,
        base_sha256: str | None,
        proposed_sha256: str,
        plan_hash: str,
        writer_fingerprint: str,
        renderer_fingerprint: str,
        structural_diff: list[dict[str, Any]],
        preview_manifest: dict[str, Any],
        warnings: list[str],
        artifacts: list[dict[str, Any]],
    ) -> None:
        now = _now()
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE document_plans_v2
                SET status = 'pending_approval', base_sha256 = ?, proposed_sha256 = ?,
                    plan_hash = ?, writer_fingerprint = ?, renderer_fingerprint = ?,
                    structural_diff_json = ?, preview_manifest_json = ?, warnings_json = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'preparing'
                """,
                (
                    base_sha256,
                    proposed_sha256,
                    plan_hash,
                    writer_fingerprint,
                    renderer_fingerprint,
                    _json(structural_diff),
                    _json(preview_manifest),
                    _json(warnings),
                    now,
                    plan_id,
                ),
            )
            if cursor.rowcount != 1:
                raise DocumentStateError("plan_state_conflict", "plan is no longer preparing")
            for artifact in artifacts:
                connection.execute(
                    """
                    INSERT INTO document_artifacts_v2(
                        id, plan_id, kind, path, sha256, mime_type, byte_size,
                        preview_label, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact["id"],
                        plan_id,
                        artifact["kind"],
                        artifact["path"],
                        artifact["sha256"],
                        artifact["mime_type"],
                        artifact["byte_size"],
                        artifact.get("preview_label"),
                        now,
                    ),
                )

    def fail_plan(self, plan_id: str, *, code: str, message: str, retryable: bool) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE document_plans_v2
                SET status = 'failed', error_code = ?, error_message = ?,
                    error_retryable = ?, updated_at = ?
                WHERE id = ? AND status = 'preparing'
                """,
                (code, message[:2_000], int(retryable), _now(), plan_id),
            )

    def get_plan(self, plan_id: str) -> StoredDocumentPlan:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM document_plans_v2 WHERE id = ?",
                (plan_id,),
            ).fetchone()
            if row is None:
                raise DocumentStateError("plan_not_found", "document change plan not found")
            if row["status"] in {"preparing", "pending_approval"} and datetime.fromisoformat(
                row["expires_at"]
            ) <= datetime.now(UTC):
                connection.execute(
                    "UPDATE document_plans_v2 SET status = 'expired', updated_at = ? WHERE id = ?",
                    (_now(), plan_id),
                )
                connection.commit()
                row = connection.execute(
                    "SELECT * FROM document_plans_v2 WHERE id = ?",
                    (plan_id,),
                ).fetchone()
                assert row is not None
            artifacts = connection.execute(
                "SELECT * FROM document_artifacts_v2 WHERE plan_id = ? ORDER BY created_at, id",
                (plan_id,),
            ).fetchall()
            return StoredDocumentPlan(_decode_plan(row), [_row(item) for item in artifacts])

    def approve_plan(
        self,
        *,
        plan_id: str,
        supplied_plan_hash: str,
        tenant_id: str,
        subject_id: str,
        actor_id: str,
        idempotency_key: str,
        request_hash: str,
        execution: dict[str, Any],
    ) -> str:
        with self._transaction() as connection:
            existing = self._idempotent_resource(
                connection,
                scope="document-plan-approve",
                tenant_id=tenant_id,
                subject_id=subject_id,
                key=idempotency_key,
                request_hash=request_hash,
            )
            if existing:
                return existing
            row = connection.execute(
                "SELECT * FROM document_plans_v2 WHERE id = ?",
                (plan_id,),
            ).fetchone()
            self._validate_owned_plan(row, tenant_id, subject_id)
            assert row is not None
            if row["status"] != "pending_approval":
                raise DocumentStateError("plan_state_conflict", "plan is not pending approval")
            if row["plan_hash"] != supplied_plan_hash:
                raise DocumentStateError("plan_hash_conflict", "plan hash does not match")
            if datetime.fromisoformat(row["expires_at"]) <= datetime.now(UTC):
                connection.execute(
                    "UPDATE document_plans_v2 SET status = 'expired', updated_at = ? WHERE id = ?",
                    (_now(), plan_id),
                )
                raise DocumentStateError("plan_expired", "plan has expired")
            now = _now()
            connection.execute(
                "UPDATE document_plans_v2 SET status = 'approved', updated_at = ? WHERE id = ?",
                (now, plan_id),
            )
            connection.execute(
                """
                INSERT INTO document_approvals_v2(
                    id, plan_id, actor_id, decision, plan_hash, created_at
                ) VALUES (?, ?, ?, 'approved', ?, ?)
                """,
                (execution["approval_id"], plan_id, actor_id, supplied_plan_hash, now),
            )
            connection.execute(
                """
                INSERT INTO document_executions_v2(
                    id, plan_id, tenant_id, subject_id, document_id, change_kind,
                    format, capability_id, source_uri, status, before_sha256,
                    after_sha256, artifact_sha256, graph_version_before, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution["id"],
                    plan_id,
                    tenant_id,
                    subject_id,
                    execution["document_id"],
                    execution["change_kind"],
                    execution["format"],
                    execution["capability_id"],
                    execution["source_uri"],
                    execution.get("before_sha256"),
                    execution["after_sha256"],
                    execution["artifact_sha256"],
                    execution["graph_version_before"],
                    now,
                    now,
                ),
            )
            self._insert_idempotency(
                connection,
                scope="document-plan-approve",
                tenant_id=tenant_id,
                subject_id=subject_id,
                key=idempotency_key,
                request_hash=request_hash,
                resource_type="document_execution",
                resource_id=execution["id"],
            )
            return str(execution["id"])

    def reject_plan(
        self,
        *,
        plan_id: str,
        supplied_plan_hash: str,
        reason: str | None,
        tenant_id: str,
        subject_id: str,
        actor_id: str,
        idempotency_key: str,
        request_hash: str,
        approval_id: str,
    ) -> str:
        with self._transaction() as connection:
            existing = self._idempotent_resource(
                connection,
                scope="document-plan-reject",
                tenant_id=tenant_id,
                subject_id=subject_id,
                key=idempotency_key,
                request_hash=request_hash,
            )
            if existing:
                return existing
            row = connection.execute(
                "SELECT * FROM document_plans_v2 WHERE id = ?",
                (plan_id,),
            ).fetchone()
            self._validate_owned_plan(row, tenant_id, subject_id)
            assert row is not None
            if row["status"] != "pending_approval" or row["plan_hash"] != supplied_plan_hash:
                raise DocumentStateError(
                    "plan_hash_conflict", "plan is stale or hash does not match"
                )
            now = _now()
            connection.execute(
                "UPDATE document_plans_v2 SET status = 'rejected', updated_at = ? WHERE id = ?",
                (now, plan_id),
            )
            connection.execute(
                """
                INSERT INTO document_approvals_v2(
                    id, plan_id, actor_id, decision, plan_hash, reason, created_at
                ) VALUES (?, ?, ?, 'rejected', ?, ?, ?)
                """,
                (approval_id, plan_id, actor_id, supplied_plan_hash, reason, now),
            )
            self._insert_idempotency(
                connection,
                scope="document-plan-reject",
                tenant_id=tenant_id,
                subject_id=subject_id,
                key=idempotency_key,
                request_hash=request_hash,
                resource_type="document_plan",
                resource_id=plan_id,
            )
            return plan_id

    def mark_file_applied(
        self,
        *,
        execution_id: str,
        backup_path: str | None,
        recovery_path: str | None,
        event_id: str,
        event_payload: dict[str, Any],
    ) -> None:
        now = _now()
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE document_executions_v2
                SET status = 'file_applied', backup_path = ?, recovery_path = ?,
                    event_id = ?, updated_at = ?
                WHERE id = ? AND status = 'prepared'
                """,
                (backup_path, recovery_path, event_id, now, execution_id),
            )
            if cursor.rowcount != 1:
                raise DocumentStateError("execution_state_conflict", "execution is not prepared")
            connection.execute(
                """
                INSERT INTO document_outbox_v2(
                    id, execution_id, event_type, payload_json, status, attempts,
                    created_at, updated_at
                ) VALUES (?, ?, 'DocumentContentChangedV2', ?, 'pending', 0, ?, ?)
                """,
                (event_id, execution_id, _json(event_payload), now, now),
            )
            connection.execute(
                """
                UPDATE document_plans_v2 SET status = 'consumed', updated_at = ?
                WHERE id = (SELECT plan_id FROM document_executions_v2 WHERE id = ?)
                """,
                (now, execution_id),
            )

    def mark_execution_error(
        self,
        execution_id: str,
        *,
        status: str,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE document_executions_v2
                SET status = ?, error_code = ?, error_message = ?,
                    error_retryable = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, code, message[:2_000], int(retryable), _now(), execution_id),
            )

    def get_execution(self, execution_id: str) -> StoredDocumentExecution:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM document_executions_v2 WHERE id = ?",
                (execution_id,),
            ).fetchone()
            if row is None:
                raise DocumentStateError("execution_not_found", "document execution not found")
            return StoredDocumentExecution(_row(row))

    def prepared_executions(self) -> list[StoredDocumentExecution]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM document_executions_v2 WHERE status = 'prepared' ORDER BY created_at"
            ).fetchall()
            return [StoredDocumentExecution(_row(row)) for row in rows]

    def idempotent_resource(
        self,
        *,
        scope: str,
        tenant_id: str,
        subject_id: str,
        key: str,
        request_hash: str,
    ) -> str | None:
        with self._connect() as connection:
            return self._idempotent_resource(
                connection,
                scope=scope,
                tenant_id=tenant_id,
                subject_id=subject_id,
                key=key,
                request_hash=request_hash,
            )

    def create_undo(
        self,
        *,
        original: dict[str, Any],
        execution_id: str,
        tenant_id: str,
        subject_id: str,
        idempotency_key: str,
        request_hash: str,
        graph_version_before: str,
    ) -> str:
        with self._transaction() as connection:
            existing = self._idempotent_resource(
                connection,
                scope="document-execution-undo",
                tenant_id=tenant_id,
                subject_id=subject_id,
                key=idempotency_key,
                request_hash=request_hash,
            )
            if existing:
                return existing
            if original["tenant_id"] != tenant_id or original["subject_id"] != subject_id:
                raise DocumentStateError("execution_not_found", "document execution not found")
            if original["status"] not in {"completed", "sync_failed", "file_applied"}:
                raise DocumentStateError("execution_state_conflict", "execution cannot be undone")
            now = _now()
            change_kind = (
                "recovery_remove" if original["change_kind"] in {"create", "derive"} else "update"
            )
            connection.execute(
                """
                INSERT INTO document_executions_v2(
                    id, undo_of_execution_id, tenant_id, subject_id, document_id,
                    change_kind, format, capability_id, source_uri, status,
                    before_sha256, after_sha256, artifact_sha256,
                    graph_version_before, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    original["id"],
                    tenant_id,
                    subject_id,
                    original["document_id"],
                    change_kind,
                    original["format"],
                    original["capability_id"],
                    original["source_uri"],
                    original["after_sha256"],
                    original["before_sha256"],
                    original["artifact_sha256"],
                    graph_version_before,
                    now,
                    now,
                ),
            )
            self._insert_idempotency(
                connection,
                scope="document-execution-undo",
                tenant_id=tenant_id,
                subject_id=subject_id,
                key=idempotency_key,
                request_hash=request_hash,
                resource_type="document_execution",
                resource_id=execution_id,
            )
            return execution_id

    def pending_events(
        self,
        *,
        limit: int = 10,
        max_attempts: int | None = None,
    ) -> list[PendingDocumentEvent]:
        with self._connect() as connection:
            attempt_clause = "" if max_attempts is None else "AND o.attempts <= ?"
            parameters: tuple[int, ...] = (
                (limit,) if max_attempts is None else (max_attempts, limit)
            )
            rows = connection.execute(
                f"""
                SELECT o.id, o.execution_id, o.payload_json, o.attempts
                FROM document_outbox_v2 o
                WHERE o.status IN ('pending', 'failed')
                {attempt_clause}
                ORDER BY o.created_at LIMIT ?
                """,
                parameters,
            ).fetchall()
            return [
                PendingDocumentEvent(
                    event_id=row["id"],
                    execution_id=row["execution_id"],
                    payload=json.loads(row["payload_json"]),
                    attempts=int(row["attempts"]),
                )
                for row in rows
            ]

    def recover_interrupted_events(self) -> int:
        """Return process-owned outbox rows to the retryable queue after a restart."""
        now = _now()
        message = "document sync was interrupted by a process restart"
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT id FROM document_outbox_v2 WHERE status = 'processing'"
            ).fetchall()
            if not rows:
                return 0
            connection.execute(
                """
                UPDATE document_executions_v2
                SET status = 'sync_failed', error_code = 'sync_interrupted',
                    error_message = ?, error_retryable = 1, updated_at = ?
                WHERE status = 'syncing' AND event_id IN (
                    SELECT id FROM document_outbox_v2 WHERE status = 'processing'
                )
                """,
                (message, now),
            )
            connection.execute(
                """
                UPDATE document_outbox_v2
                SET status = 'failed', last_error = ?, updated_at = ?
                WHERE status = 'processing'
                """,
                (message, now),
            )
            return len(rows)

    def claim_event(self, event_id: str) -> bool:
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE document_outbox_v2
                SET status = 'processing', attempts = attempts + 1, updated_at = ?
                WHERE id = ? AND status IN ('pending', 'failed')
                """,
                (_now(), event_id),
            )
            if cursor.rowcount:
                connection.execute(
                    """
                    UPDATE document_executions_v2
                    SET status = 'syncing', updated_at = ? WHERE event_id = ?
                    """,
                    (_now(), event_id),
                )
            return cursor.rowcount == 1

    def complete_event(self, event_id: str, *, graph_version_after: str) -> None:
        now = _now()
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE document_outbox_v2
                SET status = 'processed', processed_at = ?, updated_at = ?, last_error = NULL
                WHERE id = ? AND status = 'processing'
                """,
                (now, now, event_id),
            )
            connection.execute(
                """
                UPDATE document_executions_v2
                SET status = 'completed', graph_version_after = ?, error_code = NULL,
                    error_message = NULL, error_retryable = 0, updated_at = ?
                WHERE event_id = ?
                """,
                (graph_version_after, now, event_id),
            )

    def fail_event(self, event_id: str, *, code: str, message: str, retryable: bool) -> None:
        now = _now()
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE document_outbox_v2
                SET status = 'failed', last_error = ?, updated_at = ? WHERE id = ?
                """,
                (message[:2_000], now, event_id),
            )
            connection.execute(
                """
                UPDATE document_executions_v2
                SET status = 'sync_failed', error_code = ?, error_message = ?,
                    error_retryable = ?, updated_at = ? WHERE event_id = ?
                """,
                (code, message[:2_000], int(retryable), now, event_id),
            )

    def retry_event(
        self,
        execution_id: str,
        *,
        tenant_id: str,
        subject_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> str:
        with self._transaction() as connection:
            existing = self._idempotent_resource(
                connection,
                scope="retry-sync",
                tenant_id=tenant_id,
                subject_id=subject_id,
                key=idempotency_key,
                request_hash=request_hash,
            )
            if existing is not None:
                return existing
            row = connection.execute(
                "SELECT * FROM document_executions_v2 WHERE id = ?",
                (execution_id,),
            ).fetchone()
            if row is None or row["tenant_id"] != tenant_id or row["subject_id"] != subject_id:
                raise DocumentStateError("execution_not_found", "document execution not found")
            if row["status"] != "sync_failed" or not row["error_retryable"]:
                raise DocumentStateError("execution_state_conflict", "sync is not retryable")
            connection.execute(
                """
                UPDATE document_outbox_v2
                SET status = 'pending', attempts = 0, last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (_now(), row["event_id"]),
            )
            connection.execute(
                """
                UPDATE document_executions_v2 SET status = 'file_applied', error_code = NULL,
                    error_message = NULL, error_retryable = 0, updated_at = ? WHERE id = ?
                """,
                (_now(), execution_id),
            )
            self._insert_idempotency(
                connection,
                scope="retry-sync",
                tenant_id=tenant_id,
                subject_id=subject_id,
                key=idempotency_key,
                request_hash=request_hash,
                resource_type="execution",
                resource_id=execution_id,
            )
            return execution_id

    @staticmethod
    def _validate_owned_plan(
        row: sqlite3.Row | None,
        tenant_id: str,
        subject_id: str,
    ) -> None:
        if row is None or row["tenant_id"] != tenant_id or row["subject_id"] != subject_id:
            raise DocumentStateError("plan_not_found", "document change plan not found")

    @staticmethod
    def _idempotent_resource(
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
            SELECT request_hash, resource_id FROM document_idempotency_v2
            WHERE scope = ? AND tenant_id = ? AND subject_id = ? AND key = ?
            """,
            (scope, tenant_id, subject_id, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise DocumentStateError(
                "idempotency_conflict",
                "Idempotency-Key was reused with a different request",
            )
        return str(row["resource_id"])

    @staticmethod
    def _insert_idempotency(
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
            INSERT INTO document_idempotency_v2(
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
                _now(),
            ),
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
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


def _decode_plan(row: sqlite3.Row) -> dict[str, Any]:
    values = _row(row)
    for source, target in (
        ("request_json", "request"),
        ("operations_json", "operations"),
        ("structural_diff_json", "structural_diff"),
        ("preview_manifest_json", "preview_manifest"),
        ("warnings_json", "warnings"),
    ):
        raw = values.pop(source)
        values[target] = json.loads(raw) if raw else None
    return values


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}  # noqa: SIM118


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(UTC).isoformat()
