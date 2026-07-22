from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from llm_wiki_local.models import SourceEvent, SourceRecord


def _now() -> str:
    return datetime.now(UTC).isoformat()


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    source_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    active_sha256 TEXT,
                    source_version INTEGER NOT NULL,
                    last_sequence INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL,
                    fragment_files_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS source_versions (
                    source_id TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    cache_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (source_id, sha256)
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    cache_path TEXT,
                    observed_sha256 TEXT,
                    job_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    message TEXT NOT NULL,
                    error_code TEXT,
                    build_id TEXT,
                    candidate_input_dir TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS sources_doc_id_unique
                    ON sources(doc_id);
                CREATE UNIQUE INDEX IF NOT EXISTS active_sources_relative_path_unique
                    ON sources(relative_path) WHERE status = 'active';
                """
            )

    def accept_event(
        self,
        event: SourceEvent,
        *,
        job_id: str,
        cache_path: Path | None,
        observed_sha256: str | None,
    ) -> tuple[str, bool]:
        now = _now()
        payload = event.model_dump(mode="json", exclude_unset=True)
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT job_id FROM events WHERE event_id = ?", (event.event_id,)
            ).fetchone()
            if existing is not None:
                return str(existing["job_id"]), False
            connection.execute(
                """
                INSERT INTO events (
                    event_id, event_type, source_id, sequence, payload_json,
                    cache_path, observed_sha256, job_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    event.event_id,
                    event.event_type.value,
                    event.source_id,
                    event.sequence,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    str(cache_path) if cache_path else None,
                    observed_sha256,
                    job_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, event_id, status, phase, message, created_at, updated_at
                ) VALUES (?, ?, 'queued', 'queued', '작업 대기 중', ?, ?)
                """,
                (job_id, event.event_id, now, now),
            )
        return job_id, True

    def job_for_event(self, event_id: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT job_id FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return str(row["job_id"]) if row else None

    def event_for_job(self, job_id: str) -> tuple[SourceEvent, Path | None, str | None]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT e.payload_json, e.cache_path, e.observed_sha256
                FROM events e JOIN jobs j ON j.event_id = e.event_id
                WHERE j.job_id = ?
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return (
            SourceEvent.model_validate_json(row["payload_json"]),
            Path(row["cache_path"]) if row["cache_path"] else None,
            row["observed_sha256"],
        )

    def update_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        phase: str | None = None,
        message: str | None = None,
        error_code: str | None = None,
        build_id: str | None = None,
        candidate_input_dir: Path | None = None,
    ) -> None:
        fields: list[str] = ["updated_at = ?"]
        values: list[Any] = [_now()]
        for column, value in (
            ("status", status),
            ("phase", phase),
            ("message", message),
            ("error_code", error_code),
            ("build_id", build_id),
            (
                "candidate_input_dir",
                str(candidate_input_dir) if candidate_input_dir is not None else None,
            ),
        ):
            if value is not None:
                fields.append(f"{column} = ?")
                values.append(value)
        values.append(job_id)
        with self._connection() as connection:
            connection.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE job_id = ?", values)
            if status is not None:
                connection.execute(
                    """
                    UPDATE events SET status = ?, updated_at = ?
                    WHERE event_id = (SELECT event_id FROM jobs WHERE job_id = ?)
                    """,
                    (status, _now(), job_id),
                )

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT j.*, e.event_type, e.source_id, e.sequence
                FROM jobs j JOIN events e ON e.event_id = j.event_id
                WHERE j.job_id = ?
                """,
                (job_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def pending_job_ids(self) -> list[str]:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE jobs SET status = 'queued', phase = 'queued',
                    message = '재시작 후 작업 복구 중', updated_at = ?
                WHERE status = 'running'
                """,
                (_now(),),
            )
            rows = connection.execute(
                "SELECT job_id FROM jobs WHERE status = 'queued' ORDER BY created_at"
            ).fetchall()
        return [str(row["job_id"]) for row in rows]

    def source(self, source_id: str) -> SourceRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM sources WHERE source_id = ?", (source_id,)
            ).fetchone()
        if row is None:
            return None
        return SourceRecord(
            source_id=row["source_id"],
            doc_id=row["doc_id"],
            relative_path=row["relative_path"],
            status=row["status"],
            active_sha256=row["active_sha256"],
            source_version=row["source_version"],
            last_sequence=row["last_sequence"],
            metadata=json.loads(row["metadata_json"]),
            fragment_files=json.loads(row["fragment_files_json"]),
            updated_at=row["updated_at"],
        )

    def source_by_doc_id(self, doc_id: str) -> SourceRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT source_id FROM sources WHERE doc_id = ?", (doc_id,)
            ).fetchone()
        return self.source(str(row["source_id"])) if row else None

    def active_source_by_relative_path(self, relative_path: str) -> SourceRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT source_id FROM sources
                WHERE relative_path = ? AND status = 'active'
                """,
                (relative_path,),
            ).fetchone()
        return self.source(str(row["source_id"])) if row else None

    def activate_source(
        self,
        *,
        source_id: str,
        doc_id: str,
        relative_path: str,
        active_sha256: str,
        sequence: int,
        metadata: dict[str, Any],
        fragment_files: list[str],
        cache_path: Path,
    ) -> None:
        existing = self.source(source_id)
        version = (existing.source_version + 1) if existing else 1
        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO sources (
                    source_id, doc_id, relative_path, status, active_sha256,
                    source_version, last_sequence, metadata_json,
                    fragment_files_json, updated_at
                ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    doc_id = excluded.doc_id,
                    relative_path = excluded.relative_path,
                    status = 'active',
                    active_sha256 = excluded.active_sha256,
                    source_version = excluded.source_version,
                    last_sequence = excluded.last_sequence,
                    metadata_json = excluded.metadata_json,
                    fragment_files_json = excluded.fragment_files_json,
                    updated_at = excluded.updated_at
                """,
                (
                    source_id,
                    doc_id,
                    relative_path,
                    active_sha256,
                    version,
                    sequence,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    json.dumps(fragment_files, ensure_ascii=False),
                    now,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO source_versions (
                    source_id, sha256, cache_path, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (source_id, active_sha256, str(cache_path), now),
            )

    def tombstone_source(self, source: SourceRecord, *, sequence: int) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE sources SET status = 'deleted', last_sequence = ?,
                    fragment_files_json = '[]', updated_at = ?
                WHERE source_id = ?
                """,
                (sequence, _now(), source.source_id),
            )

    def advance_source_sequence(self, source_id: str, sequence: int) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE sources SET last_sequence = ?, updated_at = ?
                WHERE source_id = ? AND last_sequence < ?
                """,
                (sequence, _now(), source_id, sequence),
            )

    def set_state(self, key: str, value: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, _now()),
            )

    def get_state(self, key: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM runtime_state WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else None

    def list_sources(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM sources ORDER BY relative_path"
            ).fetchall()
        return [dict(row) for row in rows]
