from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from codegate_cloud_api.config import Settings
from codegate_cloud_api.main import create_app
from codegate_cloud_api.models import GoogleIdentity


class FakeGoogleProvider:
    def __init__(self) -> None:
        self.exchanges: list[dict[str, str]] = []

    async def exchange_code(self, **values: str) -> GoogleIdentity:
        self.exchanges.append(values)
        return GoogleIdentity(
            subject_id="google-user-123",
            email="user@example.com",
            name="테스트 사용자",
            picture="https://example.com/profile.png",
        )

    async def close(self) -> None:
        return None


def _pkce(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _settings(tmp_path) -> Settings:  # type: ignore[no-untyped-def]
    return Settings(
        environment="test",
        database_path=tmp_path / "auth.sqlite3",
        google_client_id="desktop-client.apps.googleusercontent.com",
        access_token_ttl_seconds=300,
        refresh_token_ttl_seconds=3_600,
    )


def test_google_oauth_session_lifecycle(tmp_path) -> None:  # type: ignore[no-untyped-def]
    provider = FakeGoogleProvider()
    app = create_app(_settings(tmp_path), provider=provider)
    verifier = "v" * 64
    redirect_uri = "http://127.0.0.1:47821/auth/callback"

    with TestClient(app) as client:
        started = client.post(
            "/api/v1/auth/google/start",
            json={"redirect_uri": redirect_uri, "code_challenge": _pkce(verifier)},
        )

        assert started.status_code == 200
        start_payload = started.json()
        authorization = urlsplit(start_payload["authorization_url"])
        query = parse_qs(authorization.query)
        assert authorization.scheme == "https"
        assert authorization.netloc == "accounts.google.com"
        assert query["client_id"] == ["desktop-client.apps.googleusercontent.com"]
        assert query["redirect_uri"] == [redirect_uri]
        assert query["code_challenge_method"] == ["S256"]
        assert query["scope"] == ["openid email profile"]
        assert query["state"] == [start_payload["state"]]

        exchanged = client.post(
            "/api/v1/auth/google/exchange",
            json={
                "state": start_payload["state"],
                "code": "google-authorization-code",
                "code_verifier": verifier,
            },
        )
        assert exchanged.status_code == 200
        assert exchanged.headers["Cache-Control"] == "private, no-store"
        session = exchanged.json()
        assert session["token_type"] == "bearer"
        assert session["expires_in"] == 300
        assert provider.exchanges[0]["redirect_uri"] == redirect_uri

        me = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {session['access_token']}"},
        )
        assert me.status_code == 200
        assert me.headers["Cache-Control"] == "private, no-store"
        assert me.json() == {
            "authenticated": True,
            "subject_id": "google-user-123",
            "email": "user@example.com",
            "name": "테스트 사용자",
            "picture": "https://example.com/profile.png",
        }

        refreshed = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": session["refresh_token"]},
        )
        assert refreshed.status_code == 200
        rotated = refreshed.json()
        assert rotated["access_token"] != session["access_token"]
        assert rotated["refresh_token"] != session["refresh_token"]

        old_access = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {session['access_token']}"},
        )
        assert old_access.status_code == 401
        refresh_replay = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": session["refresh_token"]},
        )
        assert refresh_replay.status_code == 401
        revoked_after_replay = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {rotated['access_token']}"},
        )
        assert revoked_after_replay.status_code == 401

        logged_out = client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {rotated['access_token']}"},
        )
        assert logged_out.status_code == 204
        after_logout = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {rotated['access_token']}"},
        )
        assert after_logout.status_code == 401


def test_oauth_state_is_one_time_and_pkce_bound(tmp_path) -> None:  # type: ignore[no-untyped-def]
    provider = FakeGoogleProvider()
    app = create_app(_settings(tmp_path), provider=provider)
    verifier = "a" * 64

    with TestClient(app) as client:
        started = client.post(
            "/api/v1/auth/google/start",
            json={
                "redirect_uri": "http://[::1]:49152/oauth/callback",
                "code_challenge": _pkce(verifier),
            },
        ).json()
        rejected = client.post(
            "/api/v1/auth/google/exchange",
            json={
                "state": started["state"],
                "code": "code",
                "code_verifier": "b" * 64,
            },
        )
        assert rejected.status_code == 401
        assert provider.exchanges == []

        replay = client.post(
            "/api/v1/auth/google/exchange",
            json={
                "state": started["state"],
                "code": "code",
                "code_verifier": verifier,
            },
        )
        assert replay.status_code == 401


def test_oauth_redirect_must_be_loopback(tmp_path) -> None:  # type: ignore[no-untyped-def]
    app = create_app(_settings(tmp_path), provider=FakeGoogleProvider())
    challenge = _pkce("v" * 64)

    with TestClient(app) as client:
        for redirect_uri in (
            "https://attacker.example/callback",
            "http://localhost:49152/callback",
            "http://127.0.0.1/callback",
            "http://127.0.0.1:49152/callback?next=evil",
        ):
            response = client.post(
                "/api/v1/auth/google/start",
                json={"redirect_uri": redirect_uri, "code_challenge": challenge},
            )
            assert response.status_code == 422


def test_unconfigured_oauth_fails_closed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(
        environment="test",
        database_path=tmp_path / "auth.sqlite3",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        health = client.get("/api/v1/health")
        assert health.json()["oauth_provider"] == "unconfigured"
        started = client.post(
            "/api/v1/auth/google/start",
            json={
                "redirect_uri": "http://127.0.0.1:49152/callback",
                "code_challenge": _pkce("v" * 64),
            },
        )
        assert started.status_code == 503


def test_auth_rejects_missing_or_malformed_bearer_token(tmp_path) -> None:  # type: ignore[no-untyped-def]
    app = create_app(_settings(tmp_path), provider=FakeGoogleProvider())

    with TestClient(app) as client:
        missing = client.get("/api/v1/auth/me")
        malformed = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Basic credentials"},
        )

    assert missing.status_code == 401
    assert missing.headers["WWW-Authenticate"] == "Bearer"
    assert malformed.status_code == 401


def test_openapi_schema_is_complete(tmp_path) -> None:  # type: ignore[no-untyped-def]
    app = create_app(_settings(tmp_path), provider=FakeGoogleProvider())

    with TestClient(app) as client:
        response = client.get("/openapi.json")

    assert response.status_code == 200
    assert "/api/v1/auth/google/start" in response.json()["paths"]


def test_oauth_start_is_rate_limited_per_client(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(tmp_path).model_copy(update={"oauth_start_rate_limit_requests": 2})
    app = create_app(settings, provider=FakeGoogleProvider())
    payload = {
        "redirect_uri": "http://127.0.0.1:49152/callback",
        "code_challenge": _pkce("v" * 64),
    }

    with TestClient(app) as client:
        assert client.post("/api/v1/auth/google/start", json=payload).status_code == 200
        assert client.post("/api/v1/auth/google/start", json=payload).status_code == 200
        limited = client.post("/api/v1/auth/google/start", json=payload)

    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"


def test_refresh_is_rate_limited_per_client(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(tmp_path).model_copy(update={"refresh_rate_limit_requests": 1})
    app = create_app(settings, provider=FakeGoogleProvider())

    with TestClient(app) as client:
        first = client.post("/api/v1/auth/refresh", json={"refresh_token": "x" * 64})
        limited = client.post("/api/v1/auth/refresh", json={"refresh_token": "y" * 64})

    assert first.status_code == 401
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"
