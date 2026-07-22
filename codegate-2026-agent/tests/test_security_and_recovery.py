from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codegate_api.config import Settings
from codegate_api.files.atomic import (
    DocumentLockManager,
    FileHashConflict,
    SafeFileError,
    SafeSourceFileStore,
)
from codegate_api.files.resolver import SourceUriResolver
from codegate_api.main import create_app
from codegate_api.state.store import StateConflict, StateStore


def test_state_store_migrates_retryable_sync_error_column(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE executions (
                id TEXT PRIMARY KEY,
                change_plan_id TEXT,
                undo_of_execution_id TEXT,
                tenant_id TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                source_uri TEXT NOT NULL,
                status TEXT NOT NULL,
                file_status TEXT NOT NULL,
                sync_status TEXT NOT NULL,
                before_sha256 TEXT NOT NULL,
                after_sha256 TEXT NOT NULL,
                backup_path TEXT NOT NULL,
                file_version_id TEXT,
                graph_version_before TEXT NOT NULL,
                graph_version_after TEXT,
                error_code TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO executions(
                id, tenant_id, subject_id, document_id, source_uri, status,
                file_status, sync_status, before_sha256, after_sha256, backup_path,
                graph_version_before, error_code, error_message, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "exec-legacy",
                "tenant",
                "subject",
                "REG-000001",
                "source://REG-000001.md",
                "file_applied",
                "applied",
                "failed",
                "0" * 64,
                "1" * 64,
                "backup",
                "version-1",
                "KNOWLEDGE_SYNC_FAILED",
                "failed",
                "2026-07-21T00:00:00+00:00",
                "2026-07-21T00:00:00+00:00",
            ),
        )

    StateStore(database).initialize()

    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(executions)")}
        retryable = connection.execute(
            "SELECT error_retryable FROM executions WHERE id = 'exec-legacy'"
        ).fetchone()
    assert "error_retryable" in columns
    assert retryable == (1,)


def test_state_store_migrates_agent_runtime_fingerprint_without_losing_session(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-agent.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE agent_runs (
                id TEXT PRIMARY KEY,
                conversation_key TEXT NOT NULL,
                status TEXT NOT NULL,
                provider TEXT NOT NULL,
                session_id TEXT,
                error_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO agent_runs(
                id, conversation_key, status, provider, session_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "run-legacy",
                "conversation-key",
                "succeeded",
                "claude-agent-sdk",
                "11111111-1111-4111-8111-111111111111",
                "2026-07-21T00:00:00+00:00",
                "2026-07-21T00:00:00+00:00",
            ),
        )

    StateStore(database).initialize()

    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_runs)")}
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(agent_runs)")}
        row = connection.execute(
            "SELECT session_id, runtime_fingerprint FROM agent_runs WHERE id = 'run-legacy'"
        ).fetchone()
        migration = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 5"
        ).fetchone()

    assert "runtime_fingerprint" in columns
    assert "idx_agent_runs_session_resume" in indexes
    assert row == ("11111111-1111-4111-8111-111111111111", None)
    assert migration == (1,)


def test_state_store_rejects_changed_validated_llmwiki_digest(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite3")
    state.initialize()
    state.record_llmwiki_build_digest(
        build_id="build-0123456789abcdefabcd",
        native_digest="a" * 64,
        parent_build_id="build-fedcba9876543210abcd",
        status="validated",
    )

    with pytest.raises(StateConflict, match="변경"):
        state.record_llmwiki_build_digest(
            build_id="build-0123456789abcdefabcd",
            native_digest="b" * 64,
            parent_build_id="build-fedcba9876543210abcd",
            status="active",
        )

    assert state.llmwiki_build_digest("build-0123456789abcdefabcd") == "a" * 64


def test_publish_barrier_waits_for_source_write(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = DocumentLockManager(tmp_path / "locks")
        source_entered = asyncio.Event()
        release_source = asyncio.Event()
        publish_entered = asyncio.Event()

        async def hold_source_write() -> None:
            async with manager.acquire("REG-000001"):
                source_entered.set()
                await release_source.wait()

        async def publish() -> None:
            async with manager.acquire_publish():
                publish_entered.set()

        source_task = asyncio.create_task(hold_source_write())
        await source_entered.wait()
        publish_task = asyncio.create_task(publish())
        await asyncio.sleep(0)
        assert not publish_entered.is_set()
        release_source.set()
        await asyncio.gather(source_task, publish_task)
        assert publish_entered.is_set()

    asyncio.run(scenario())


def _preview(client: TestClient, headers: dict[str, str]) -> dict[str, str]:
    response = client.post(
        "/api/v1/chat/messages",
        headers=headers,
        json={
            "conversation_id": "security-recovery",
            "message": 'REG-000001에서 "1년"을 "3년"으로 변경해줘',
            "selected_document_id": "REG-000001",
        },
    )
    return response.json()["change_plan"]


def _wait_for_execution(
    client: TestClient,
    headers: dict[str, str],
    execution_id: str,
    *,
    sync_status: str,
) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/executions/{execution_id}", headers=headers)
        assert response.status_code == 200
        execution = response.json()
        if execution["sync_status"] == sync_status:
            return execution
        time.sleep(0.01)
    raise AssertionError(f"execution did not reach sync status {sync_status}")


def test_file_and_parent_symlinks_are_rejected(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    outside = tmp_path / "outside"
    backup_root = tmp_path / "backups"
    source_root.mkdir()
    outside.mkdir()
    sentinel = outside / "sentinel.md"
    sentinel.write_text("outside", encoding="utf-8")
    (source_root / "file-link.md").symlink_to(sentinel)
    (source_root / "parent-link").symlink_to(outside, target_is_directory=True)
    files = SafeSourceFileStore(
        SourceUriResolver(source_root),
        backup_root=backup_root,
        max_bytes=1024,
    )

    with pytest.raises(SafeFileError, match="안전하게 열 수 없습니다"):
        files.snapshot("source://file-link.md")
    with pytest.raises(SafeFileError, match="안전하게 열 수 없습니다"):
        files.snapshot("source://parent-link/sentinel.md")
    assert sentinel.read_text(encoding="utf-8") == "outside"


def test_backup_symlink_swap_is_rejected(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "example.md"
    source.write_text("trusted backup\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("untrusted replacement\n", encoding="utf-8")
    files = SafeSourceFileStore(
        SourceUriResolver(source_root),
        backup_root=tmp_path / "backups",
        max_bytes=1024,
    )
    snapshot = files.snapshot("source://example.md")
    backup = files.write_backup(f"exec_{'a' * 32}", snapshot)
    backup.unlink()
    backup.symlink_to(outside)

    with pytest.raises(SafeFileError):
        files.read_backup(str(backup))


def test_retention_prunes_only_expired_app_managed_execution_directories(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    backup_root = tmp_path / "backups"
    recovery_root = tmp_path / "recovery"
    source_root.mkdir()
    files = SafeSourceFileStore(
        SourceUriResolver(source_root),
        backup_root=backup_root,
        recovery_root=recovery_root,
        max_bytes=1024,
    )
    expired = backup_root / f"exec_{'a' * 32}"
    current = recovery_root / f"exec_{'b' * 32}"
    unmanaged = backup_root / "do-not-delete"
    for directory in (expired, current, unmanaged):
        directory.mkdir(parents=True)
        (directory / "artifact.bin").write_bytes(b"managed")
    old_timestamp = (datetime.now(UTC) - timedelta(days=31)).timestamp()
    os.utime(expired, (old_timestamp, old_timestamp))
    os.utime(unmanaged, (old_timestamp, old_timestamp))

    removed = files.prune_expired_managed_files(older_than=datetime.now(UTC) - timedelta(days=30))

    assert removed == [expired]
    assert not expired.exists()
    assert current.is_dir()
    assert unmanaged.is_dir()


def test_atomic_replace_never_exposes_partial_content(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "example.md"
    source.write_bytes(b"old content\n")
    files = SafeSourceFileStore(
        SourceUriResolver(source_root),
        backup_root=tmp_path / "backups",
        max_bytes=1024,
    )
    before = files.snapshot("source://example.md")

    after = files.atomic_replace(
        source_uri="source://example.md",
        expected_sha256=before.sha256,
        new_content=b"complete new content\n",
    )

    assert source.read_bytes() == b"complete new content\n"
    assert after.sha256 != before.sha256
    assert not list(source_root.glob(".codegate-*.tmp"))


def test_atomic_replace_rechecks_external_edit_before_rename(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "example.md"
    source.write_bytes(b"old content\n")

    def external_write(stage: str) -> None:
        if stage == "temp_fsynced":
            source.write_bytes(b"external edit\n")

    files = SafeSourceFileStore(
        SourceUriResolver(source_root),
        backup_root=tmp_path / "backups",
        max_bytes=1024,
        fault_hook=external_write,
    )
    before = files.snapshot("source://example.md")

    with pytest.raises(FileHashConflict):
        files.atomic_replace(
            source_uri="source://example.md",
            expected_sha256=before.sha256,
            new_content=b"approved edit\n",
        )

    assert source.read_bytes() == b"external edit\n"
    assert not list(source_root.glob(".codegate-*.tmp"))


def test_fanout_failure_keeps_old_version_then_retry_publishes(
    client: TestClient,
    auth_headers: dict[str, str],
    test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_snapshot = test_app.state.container.catalog.snapshot()
    pipeline = test_app.state.container.pipeline
    pipeline._fail_stage = "vector"  # noqa: SLF001
    fts_finished = threading.Event()
    original_build_fts = pipeline._build_fts  # noqa: SLF001

    def slow_build_fts(candidate: Path) -> None:
        time.sleep(0.1)
        original_build_fts(candidate)
        fts_finished.set()

    monkeypatch.setattr(pipeline, "_build_fts", slow_build_fts)
    plan = _preview(client, auth_headers)
    response = client.post(
        f"/api/v1/change-plans/{plan['change_plan_id']}/approve",
        headers={**auth_headers, "Idempotency-Key": "fanout-failure-key"},
        json={"plan_hash": plan["plan_hash"]},
    )

    assert response.status_code == 202
    pending = response.json()
    execution = _wait_for_execution(
        client,
        auth_headers,
        str(pending["execution_id"]),
        sync_status="failed",
    )
    assert execution["file_status"] == "applied"
    assert execution["sync_status"] == "failed"
    assert execution["error"]["retryable"] is True  # type: ignore[index]
    assert fts_finished.is_set()
    assert test_app.state.container.catalog.snapshot() is old_snapshot

    pipeline._fail_stage = None  # noqa: SLF001
    retried = client.post(
        f"/api/v1/executions/{execution['execution_id']}/retry-sync",
        headers=auth_headers,
    )
    assert retried.status_code == 202
    refreshed = _wait_for_execution(
        client,
        auth_headers,
        str(execution["execution_id"]),
        sync_status="published",
    )
    assert refreshed["status"] == "completed"
    assert refreshed["graph_version_after"] != execution["graph_version_before"]
    assert (
        "3년"
        in test_app.state.container.catalog.snapshot()
        .get(
            "REG-000001",
            access_context=test_app.state.container.authenticator.authenticate(
                auth_headers["Authorization"].removeprefix("Bearer ")
            ),
        )
        .evidence[0]
        .quote
    )  # type: ignore[union-attr]


def test_transient_worker_error_retries_pending_events_without_a_new_notification(
    client: TestClient,
    auth_headers: dict[str, str],
    test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = test_app.state.container.pipeline
    original_process_pending = pipeline.process_pending
    attempts = 0

    async def fail_once_before_state_transition() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated transient state-store failure")
        return await original_process_pending()

    monkeypatch.setattr(pipeline, "process_pending", fail_once_before_state_transition)
    plan = _preview(client, auth_headers)
    approved = client.post(
        f"/api/v1/change-plans/{plan['change_plan_id']}/approve",
        headers={**auth_headers, "Idempotency-Key": "worker-transient-key"},
        json={"plan_hash": plan["plan_hash"]},
    )
    published = _wait_for_execution(
        client,
        auth_headers,
        str(approved.json()["execution_id"]),
        sync_status="published",
    )

    assert attempts >= 2
    assert published["status"] == "completed"
    assert test_app.state.container.sync_worker.last_progress_at is not None


def test_unexpected_adapter_error_does_not_kill_worker(
    client: TestClient,
    auth_headers: dict[str, str],
    test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = test_app.state.container.pipeline
    original_build_graph = pipeline._build_graph  # noqa: SLF001

    def crash_graph(_: Path) -> None:
        raise RuntimeError("unexpected graph adapter crash")

    monkeypatch.setattr(pipeline, "_build_graph", crash_graph)
    plan = _preview(client, auth_headers)
    approved = client.post(
        f"/api/v1/change-plans/{plan['change_plan_id']}/approve",
        headers={**auth_headers, "Idempotency-Key": "unexpected-adapter-key"},
        json={"plan_hash": plan["plan_hash"]},
    )
    failed = _wait_for_execution(
        client,
        auth_headers,
        str(approved.json()["execution_id"]),
        sync_status="failed",
    )

    assert failed["terminal"] is True
    assert failed["recommended_poll_after_ms"] is None
    assert test_app.state.container.sync_worker.healthy is True
    health = client.get("/api/v1/health").json()
    assert health["status"] == "degraded"
    assert health["worker_available"] is True
    assert health["failed_sync_events"] == 1

    monkeypatch.setattr(pipeline, "_build_graph", original_build_graph)
    retried = client.post(
        f"/api/v1/executions/{failed['execution_id']}/retry-sync",
        headers=auth_headers,
    )
    assert retried.status_code == 202
    published = _wait_for_execution(
        client,
        auth_headers,
        str(failed["execution_id"]),
        sync_status="published",
    )
    assert published["status"] == "completed"


def test_sync_failed_execution_can_be_compensated_by_undo(
    client: TestClient,
    auth_headers: dict[str, str],
    app_settings: Settings,
    test_app: FastAPI,
) -> None:
    source = app_settings.resolved_source_root() / "regulations/REG-000001.md"
    original = source.read_bytes()
    test_app.state.container.pipeline._fail_stage = "vector"  # noqa: SLF001
    plan = _preview(client, auth_headers)
    approved = client.post(
        f"/api/v1/change-plans/{plan['change_plan_id']}/approve",
        headers={**auth_headers, "Idempotency-Key": "failed-undo-approve"},
        json={"plan_hash": plan["plan_hash"]},
    )
    failed = _wait_for_execution(
        client,
        auth_headers,
        str(approved.json()["execution_id"]),
        sync_status="failed",
    )
    test_app.state.container.pipeline._fail_stage = None  # noqa: SLF001

    undone = client.post(
        f"/api/v1/executions/{failed['execution_id']}/undo",
        headers={**auth_headers, "Idempotency-Key": "failed-undo-key"},
    )

    assert undone.status_code == 202
    compensated = _wait_for_execution(
        client,
        auth_headers,
        str(undone.json()["execution_id"]),
        sync_status="published",
    )
    assert compensated["status"] == "completed"
    assert source.read_bytes() == original
    assert client.get("/api/v1/health").json()["failed_sync_events"] == 0


def test_undo_file_write_failure_can_resume_with_a_new_idempotency_key(
    client: TestClient,
    auth_headers: dict[str, str],
    app_settings: Settings,
    test_app: FastAPI,
) -> None:
    source = app_settings.resolved_source_root() / "regulations/REG-000001.md"
    original = source.read_bytes()
    plan = _preview(client, auth_headers)
    approved = client.post(
        f"/api/v1/change-plans/{plan['change_plan_id']}/approve",
        headers={**auth_headers, "Idempotency-Key": "undo-write-approve"},
        json={"plan_hash": plan["plan_hash"]},
    ).json()
    completed = _wait_for_execution(
        client,
        auth_headers,
        str(approved["execution_id"]),
        sync_status="published",
    )

    failed_once = False

    def fail_before_replace(stage: str) -> None:
        nonlocal failed_once
        if stage == "temp_fsynced" and not failed_once:
            failed_once = True
            raise OSError("simulated transient storage failure")

    test_app.state.container.files._fault_hook = fail_before_replace  # noqa: SLF001
    failed = client.post(
        f"/api/v1/executions/{completed['execution_id']}/undo",
        headers={**auth_headers, "Idempotency-Key": "undo-write-first"},
    )

    assert failed.status_code == 422
    assert failed.json()["detail"]["code"] == "ATOMIC_WRITE_FAILED"
    assert source.read_bytes() != original
    prepared_undo = test_app.state.container.state.undo_execution_for(
        str(completed["execution_id"]),
        tenant_id="demo",
        subject_id="demo-editor",
    )
    assert prepared_undo is not None
    assert prepared_undo.view.file_status.value == "failed"

    test_app.state.container.files._fault_hook = lambda _: None  # noqa: SLF001
    retried = client.post(
        f"/api/v1/executions/{completed['execution_id']}/undo",
        headers={**auth_headers, "Idempotency-Key": "undo-write-second"},
    )

    assert retried.status_code == 202
    assert retried.json()["execution_id"] == prepared_undo.view.execution_id
    recovered = _wait_for_execution(
        client,
        auth_headers,
        prepared_undo.view.execution_id,
        sync_status="published",
    )
    assert recovered["status"] == "completed"
    assert source.read_bytes() == original


def test_approval_file_write_failure_can_resume_consumed_plan_with_a_new_key(
    client: TestClient,
    auth_headers: dict[str, str],
    app_settings: Settings,
    test_app: FastAPI,
) -> None:
    source = app_settings.resolved_source_root() / "regulations/REG-000001.md"
    original = source.read_bytes()
    plan = _preview(client, auth_headers)
    failed_once = False

    def fail_before_replace(stage: str) -> None:
        nonlocal failed_once
        if stage == "temp_fsynced" and not failed_once:
            failed_once = True
            raise OSError("simulated transient storage failure")

    test_app.state.container.files._fault_hook = fail_before_replace  # noqa: SLF001
    failed = client.post(
        f"/api/v1/change-plans/{plan['change_plan_id']}/approve",
        headers={**auth_headers, "Idempotency-Key": "approval-write-first"},
        json={"plan_hash": plan["plan_hash"]},
    )

    assert failed.status_code == 422
    assert source.read_bytes() == original
    prepared = test_app.state.container.state.execution_for_plan(
        str(plan["change_plan_id"]),
        tenant_id="demo",
        subject_id="demo-editor",
    )
    assert prepared is not None
    assert prepared.view.file_status.value == "failed"

    test_app.state.container.files._fault_hook = lambda _: None  # noqa: SLF001
    retried = client.post(
        f"/api/v1/change-plans/{plan['change_plan_id']}/approve",
        headers={**auth_headers, "Idempotency-Key": "approval-write-second"},
        json={"plan_hash": plan["plan_hash"]},
    )

    assert retried.status_code == 202
    assert retried.json()["execution_id"] == prepared.view.execution_id
    recovered = _wait_for_execution(
        client,
        auth_headers,
        prepared.view.execution_id,
        sync_status="published",
    )
    assert recovered["status"] == "completed"
    assert source.read_bytes() != original


def test_startup_recovers_crash_after_replace(
    app_settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    first_app = create_app(app_settings)
    with TestClient(first_app) as first_client:
        plan = _preview(first_client, auth_headers)

        def crash_after_replace(stage: str) -> None:
            if stage == "replaced":
                raise RuntimeError("simulated process crash")

        first_app.state.container.files._fault_hook = crash_after_replace  # noqa: SLF001
        with pytest.raises(RuntimeError, match="simulated process crash"):
            first_client.post(
                f"/api/v1/change-plans/{plan['change_plan_id']}/approve",
                headers={**auth_headers, "Idempotency-Key": "crash-recovery-key"},
                json={"plan_hash": plan["plan_hash"]},
            )
        source = app_settings.resolved_source_root() / "regulations/REG-000001.md"
        assert "3년" in source.read_text(encoding="utf-8")
        assert not list(source.parent.glob(".codegate-*.tmp"))

    recovered_app = create_app(app_settings)
    with TestClient(recovered_app) as recovered_client:
        replay = recovered_client.post(
            f"/api/v1/change-plans/{plan['change_plan_id']}/approve",
            headers={**auth_headers, "Idempotency-Key": "crash-recovery-key"},
            json={"plan_hash": plan["plan_hash"]},
        )
        assert replay.status_code == 202
        recovered = _wait_for_execution(
            recovered_client,
            auth_headers,
            str(replay.json()["execution_id"]),
            sync_status="published",
        )
        assert recovered["status"] == "completed"

        source_mode = os.stat(
            app_settings.resolved_source_root() / "regulations/REG-000001.md"
        ).st_mode
        assert source_mode
