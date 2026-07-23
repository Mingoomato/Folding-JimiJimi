from __future__ import annotations

import sqlite3
from pathlib import Path

from codegate_api.documents.store import DocumentStateStore
from codegate_api.state.store import StateStore


def test_processing_outbox_is_recovered_after_process_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    StateStore(database).initialize()
    now = "2026-07-23T00:00:00Z"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO document_executions_v2(
                id, tenant_id, subject_id, document_id, change_kind, format,
                capability_id, source_uri, status, after_sha256, artifact_sha256,
                graph_version_before, event_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'syncing', ?, ?, ?, ?, ?, ?)
            """,
            (
                "exec-restart",
                "local",
                "user",
                "DOC-RESTART",
                "derive",
                "hwpx",
                "hwp.derive.kordoc/v1",
                "source://일일업무.hwpx",
                "a" * 64,
                "a" * 64,
                "build-before",
                "event-restart",
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO document_outbox_v2(
                id, execution_id, event_type, payload_json, status, attempts,
                created_at, updated_at
            ) VALUES (?, ?, 'DocumentContentChangedV2', '{}', 'processing', 1, ?, ?)
            """,
            ("event-restart", "exec-restart", now, now),
        )
        connection.commit()

    store = DocumentStateStore(database)
    assert store.recover_interrupted_events() == 1
    assert store.recover_interrupted_events() == 0

    execution = store.get_execution("exec-restart").values
    assert execution["status"] == "sync_failed"
    assert execution["error_code"] == "sync_interrupted"
    assert execution["error_retryable"] == 1
    pending = store.pending_events()
    assert [event.event_id for event in pending] == ["event-restart"]

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE document_outbox_v2 SET attempts = 4 WHERE id = 'event-restart'"
        )
        connection.commit()
    assert store.pending_events(max_attempts=3) == []

    assert (
        store.retry_event(
            "exec-restart",
            tenant_id="local",
            subject_id="user",
            idempotency_key="retry-after-exhaustion",
            request_hash="b" * 64,
        )
        == "exec-restart"
    )
    retried = store.pending_events(max_attempts=3)
    assert [(event.event_id, event.attempts) for event in retried] == [
        ("event-restart", 0)
    ]
