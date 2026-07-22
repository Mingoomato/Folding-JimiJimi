import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from codegate_cloud_api.models import GoogleIdentity
from codegate_cloud_api.store import AuthStore, OAuthFlowCapacityError


def test_refresh_replay_revokes_the_rotated_token_family(tmp_path) -> None:  # type: ignore[no-untyped-def]
    database_path = tmp_path / "auth.sqlite3"
    pepper = b"test-session-pepper-not-for-production"
    first_store = AuthStore(database_path, pepper)
    second_store = AuthStore(database_path, pepper)
    identity = GoogleIdentity(subject_id="user-1", email="user@example.com")
    _, refresh_token = first_store.create_session(
        identity,
        access_ttl_seconds=300,
        refresh_ttl_seconds=3_600,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                store.rotate_session,
                refresh_token,
                access_ttl_seconds=300,
            )
            for store in (first_store, second_store)
        ]
        results = [future.result() for future in futures]

    successful = [result for result in results if result is not None]
    assert len(successful) == 1
    assert first_store.user_for_access_token(successful[0][0]) is None
    assert second_store.user_for_access_token(successful[0][0]) is None
    first_store.close()
    second_store.close()


def test_pending_oauth_flow_capacity_is_bounded(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = AuthStore(
        tmp_path / "auth.sqlite3",
        b"test-session-pepper-not-for-production",
        max_pending_oauth_flows=1,
    )
    store.create_oauth_flow(
        redirect_uri="http://127.0.0.1:49152/callback",
        code_challenge="c" * 43,
        ttl_seconds=300,
    )

    with pytest.raises(OAuthFlowCapacityError):
        store.create_oauth_flow(
            redirect_uri="http://127.0.0.1:49153/callback",
            code_challenge="d" * 43,
            ttl_seconds=300,
        )

    store.close()


def test_refresh_rotation_quota_bounds_replay_history(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = AuthStore(
        tmp_path / "auth.sqlite3",
        b"test-session-pepper-not-for-production",
        max_refresh_rotations_per_family=1,
    )
    _, original_refresh = store.create_session(
        GoogleIdentity(subject_id="user-1", email="user@example.com"),
        access_ttl_seconds=300,
        refresh_ttl_seconds=3_600,
    )
    rotated = store.rotate_session(original_refresh, access_ttl_seconds=300)
    assert rotated is not None

    assert store.rotate_session(rotated[1], access_ttl_seconds=300) is None
    assert store.user_for_access_token(rotated[0]) is None
    store.close()


def test_expired_cleanup_removes_orphaned_user_pii(tmp_path) -> None:  # type: ignore[no-untyped-def]
    database_path = tmp_path / "auth.sqlite3"
    store = AuthStore(
        database_path,
        b"test-session-pepper-not-for-production",
    )
    store.create_session(
        GoogleIdentity(
            subject_id="user-1",
            email="user@example.com",
            name="Test User",
            picture="https://example.com/picture.png",
        ),
        access_ttl_seconds=300,
        refresh_ttl_seconds=3_600,
    )
    expired_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE sessions SET refresh_expires_at = ?, family_expires_at = ?",
            (expired_at, expired_at),
        )

    store.cleanup_expired()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    store.close()
