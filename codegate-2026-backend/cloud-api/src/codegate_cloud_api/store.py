from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from codegate_cloud_api.models import GoogleIdentity


@dataclass(frozen=True)
class OAuthFlow:
    state: str
    redirect_uri: str
    code_challenge: str
    nonce: str
    expires_at: datetime


@dataclass(frozen=True)
class StoredUser:
    subject_id: str
    email: str
    name: str | None
    picture: str | None


class OAuthFlowCapacityError(RuntimeError):
    pass


class AuthStore:
    def __init__(
        self,
        database_path: Path,
        pepper: bytes,
        *,
        max_pending_oauth_flows: int = 10_000,
        max_refresh_rotations_per_family: int = 2_048,
    ) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        if os.name == "posix":
            database_path.chmod(0o600)
        self._connection.row_factory = sqlite3.Row
        self._pepper = pepper
        self._max_pending_oauth_flows = max_pending_oauth_flows
        self._max_refresh_rotations_per_family = max_refresh_rotations_per_family
        self._lock = threading.RLock()
        with self._connection:
            self._connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS oauth_flows (
                    state_hash TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS users (
                    subject_id TEXT PRIMARY KEY,
                    email TEXT NOT NULL,
                    name TEXT,
                    picture TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    refresh_hash TEXT PRIMARY KEY,
                    access_hash TEXT UNIQUE NOT NULL,
                    family_id TEXT NOT NULL,
                    subject_id TEXT NOT NULL REFERENCES users(subject_id) ON DELETE CASCADE,
                    access_expires_at TEXT NOT NULL,
                    refresh_expires_at TEXT NOT NULL,
                    family_expires_at TEXT NOT NULL,
                    rotation_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS used_refresh_tokens (
                    refresh_hash TEXT PRIMARY KEY,
                    family_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS sessions_access_hash_idx
                    ON sessions(access_hash);

                CREATE INDEX IF NOT EXISTS sessions_family_id_idx
                    ON sessions(family_id);
                """
            )
            self._cleanup_expired(datetime.now(UTC))

    def close(self) -> None:
        self._connection.close()

    def create_oauth_flow(
        self,
        *,
        redirect_uri: str,
        code_challenge: str,
        ttl_seconds: int,
    ) -> OAuthFlow:
        now = datetime.now(UTC)
        flow = OAuthFlow(
            state=secrets.token_urlsafe(32),
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            nonce=secrets.token_urlsafe(32),
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        payload = json.dumps(
            {
                "redirect_uri": flow.redirect_uri,
                "code_challenge": flow.code_challenge,
                "nonce": flow.nonce,
            },
            separators=(",", ":"),
        )
        with self._lock, self._connection:
            self._cleanup_expired(now)
            pending_count = self._connection.execute("SELECT COUNT(*) FROM oauth_flows").fetchone()[
                0
            ]
            if pending_count >= self._max_pending_oauth_flows:
                raise OAuthFlowCapacityError("too many pending OAuth flows")
            self._connection.execute(
                "INSERT INTO oauth_flows(state_hash, payload, expires_at) VALUES (?, ?, ?)",
                (self._digest(flow.state), payload, flow.expires_at.isoformat()),
            )
        return flow

    def consume_oauth_flow(self, state: str) -> OAuthFlow | None:
        state_hash = self._digest(state)
        now = datetime.now(UTC)
        with self._lock, self._connection:
            row = self._connection.execute(
                """
                DELETE FROM oauth_flows
                WHERE state_hash = ?
                RETURNING payload, expires_at
                """,
                (state_hash,),
            ).fetchone()
        if row is None:
            return None
        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at <= now:
            return None
        payload = json.loads(row["payload"])
        return OAuthFlow(
            state=state,
            redirect_uri=payload["redirect_uri"],
            code_challenge=payload["code_challenge"],
            nonce=payload["nonce"],
            expires_at=expires_at,
        )

    def create_session(
        self,
        identity: GoogleIdentity,
        *,
        access_ttl_seconds: int,
        refresh_ttl_seconds: int,
    ) -> tuple[str, str]:
        now = datetime.now(UTC)
        access_token = secrets.token_urlsafe(48)
        refresh_token = secrets.token_urlsafe(64)
        family_id = secrets.token_urlsafe(24)
        family_expires_at = now + timedelta(seconds=refresh_ttl_seconds)
        with self._lock, self._connection:
            self._cleanup_expired(now)
            self._connection.execute(
                """
                INSERT INTO users(subject_id, email, name, picture, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(subject_id) DO UPDATE SET
                    email = excluded.email,
                    name = excluded.name,
                    picture = excluded.picture,
                    updated_at = excluded.updated_at
                """,
                (
                    identity.subject_id,
                    identity.email,
                    identity.name,
                    identity.picture,
                    now.isoformat(),
                ),
            )
            self._connection.execute(
                """
                INSERT INTO sessions(
                    refresh_hash, access_hash, family_id, subject_id,
                    access_expires_at, refresh_expires_at, family_expires_at,
                    rotation_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._digest(refresh_token),
                    self._digest(access_token),
                    family_id,
                    identity.subject_id,
                    (now + timedelta(seconds=access_ttl_seconds)).isoformat(),
                    family_expires_at.isoformat(),
                    family_expires_at.isoformat(),
                    0,
                    now.isoformat(),
                ),
            )
        return access_token, refresh_token

    def user_for_access_token(self, access_token: str) -> StoredUser | None:
        now = datetime.now(UTC).isoformat()
        with self._lock:
            row = self._connection.execute(
                """
                SELECT u.subject_id, u.email, u.name, u.picture
                FROM sessions AS s
                JOIN users AS u ON u.subject_id = s.subject_id
                WHERE s.access_hash = ?
                  AND s.access_expires_at > ?
                  AND s.family_expires_at > ?
                """,
                (self._digest(access_token), now, now),
            ).fetchone()
        return StoredUser(**dict(row)) if row is not None else None

    def rotate_session(
        self,
        refresh_token: str,
        *,
        access_ttl_seconds: int,
    ) -> tuple[str, str] | None:
        refresh_hash = self._digest(refresh_token)
        now = datetime.now(UTC)
        new_access_token = secrets.token_urlsafe(48)
        new_refresh_token = secrets.token_urlsafe(64)
        with self._lock, self._connection:
            self._cleanup_expired(now)
            row = self._connection.execute(
                """
                DELETE FROM sessions
                WHERE refresh_hash = ? AND refresh_expires_at > ?
                RETURNING subject_id, family_id, family_expires_at, rotation_count
                """,
                (refresh_hash, now.isoformat()),
            ).fetchone()
            if row is None:
                replay = self._connection.execute(
                    """
                    SELECT family_id FROM used_refresh_tokens
                    WHERE refresh_hash = ? AND expires_at > ?
                    """,
                    (refresh_hash, now.isoformat()),
                ).fetchone()
                if replay is not None:
                    self._connection.execute(
                        "DELETE FROM sessions WHERE family_id = ?",
                        (replay["family_id"],),
                    )
                    self._delete_orphan_users()
                return None
            if row["rotation_count"] >= self._max_refresh_rotations_per_family:
                self._connection.execute(
                    "DELETE FROM used_refresh_tokens WHERE family_id = ?",
                    (row["family_id"],),
                )
                self._delete_orphan_users()
                return None
            self._connection.execute(
                """
                INSERT INTO used_refresh_tokens(refresh_hash, family_id, expires_at)
                VALUES (?, ?, ?)
                """,
                (refresh_hash, row["family_id"], row["family_expires_at"]),
            )
            family_expires_at = datetime.fromisoformat(row["family_expires_at"])
            access_expires_at = min(
                now + timedelta(seconds=access_ttl_seconds),
                family_expires_at,
            )
            self._connection.execute(
                """
                INSERT INTO sessions(
                    refresh_hash, access_hash, family_id, subject_id,
                    access_expires_at, refresh_expires_at, family_expires_at,
                    rotation_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._digest(new_refresh_token),
                    self._digest(new_access_token),
                    row["family_id"],
                    row["subject_id"],
                    access_expires_at.isoformat(),
                    row["family_expires_at"],
                    row["family_expires_at"],
                    row["rotation_count"] + 1,
                    now.isoformat(),
                ),
            )
        return new_access_token, new_refresh_token

    def revoke_access_token(self, access_token: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM sessions WHERE access_hash = ?",
                (self._digest(access_token),),
            )
            self._cleanup_expired(datetime.now(UTC))

    def cleanup_expired(self) -> None:
        with self._lock, self._connection:
            self._cleanup_expired(datetime.now(UTC))

    def _cleanup_expired(self, now: datetime) -> None:
        self._connection.execute(
            "DELETE FROM oauth_flows WHERE expires_at <= ?",
            (now.isoformat(),),
        )
        self._connection.execute(
            "DELETE FROM sessions WHERE refresh_expires_at <= ?",
            (now.isoformat(),),
        )
        self._connection.execute(
            "DELETE FROM used_refresh_tokens WHERE expires_at <= ?",
            (now.isoformat(),),
        )
        self._delete_orphan_users()

    def _delete_orphan_users(self) -> None:
        self._connection.execute(
            "DELETE FROM users WHERE NOT EXISTS "
            "(SELECT 1 FROM sessions WHERE sessions.subject_id = users.subject_id)"
        )

    def _digest(self, value: str) -> str:
        return hmac.new(self._pepper, value.encode(), hashlib.sha256).hexdigest()


def verify_pkce(code_verifier: str, expected_challenge: str) -> bool:
    digest = hashlib.sha256(code_verifier.encode()).digest()
    actual = urlsafe_b64encode(digest).rstrip(b"=").decode()
    return hmac.compare_digest(actual, expected_challenge)
