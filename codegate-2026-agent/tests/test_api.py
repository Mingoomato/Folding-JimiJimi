from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_auth_me_returns_frontend_session_contract(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    response = client.get("/api/v1/auth/me", headers=auth_headers)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.json() == {
        "authenticated": True,
        "subject_id": "demo-editor",
        "tenant_id": "demo",
        "email": None,
        "read_access": ["internal", "public"],
        "write_scope": "documents",
        "writable_document_ids": [
            "MAN-000001",
            "MAN-000002",
            "REG-000001",
            "REG-000002",
        ],
        "provisioned": True,
        "authz_source": "demo-token",
    }
    assert "access_token" not in response.json()
    assert "refresh_token" not in response.json()


def test_auth_me_requires_login(client: TestClient) -> None:
    response = client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["detail"]["code"] == "UNAUTHORIZED"


def test_auth_cors_uses_bearer_headers_without_credentials(client: TestClient) -> None:
    response = client.options(
        "/api/v1/auth/me",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )

    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:3000"
    assert "Access-Control-Allow-Credentials" not in response.headers


def test_local_mode_rejects_non_loopback_clients(test_app: FastAPI) -> None:
    test_app.state.container.settings.environment = "local"

    with TestClient(test_app, client=("192.0.2.10", 50000)) as local_client:
        response = local_client.get("/api/v1/health")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "LOCAL_ACCESS_ONLY"


def test_health_reports_loaded_knowledge_version(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "knowledge_version": "2026-07-21.2-demo",
        "converter_available": True,
        "graph_available": True,
        "index_artifacts_available": False,
        "worker_available": True,
        "pending_sync_events": 0,
        "failed_sync_events": 0,
        "stale_sync_events": 0,
        "agent_available": True,
        "auth_mode": "demo",
        "persistence_available": True,
    }


def test_production_health_fails_when_converter_is_unavailable(
    client: TestClient,
    test_app: FastAPI,
) -> None:
    test_app.state.container.doc2md = SimpleNamespace(health=lambda: False)
    test_app.state.container.settings.environment = "production"

    response = client.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["converter_available"] is False


def test_health_degrades_only_when_pending_sync_is_stale(
    client: TestClient,
    test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        test_app.state.container.state,
        "stale_sync_event_count",
        lambda *, stale_after_seconds: int(stale_after_seconds > 0),
    )

    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["stale_sync_events"] == 1


def test_chat_locates_document_with_evidence(client: TestClient) -> None:
    response = client.post(
        "/api/v1/chat/messages",
        headers={"Idempotency-Key": "00000000-0000-0000-0000-000000000001"},
        json={
            "conversation_id": "conv_demo",
            "message": "개인정보 보관 기간 문서 어디 있어?",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["response_type"] == "location_result"
    assert payload["documents"][0]["document_id"] == "REG-000001"
    assert payload["documents"][0]["evidence"][0]["section"] == "제4조 보관 기간"
    assert payload["documents"][0]["graph_version"] == "2026-07-21.2-demo"
    document = payload["documents"][0]
    assert document["revision"] == "1"
    assert document["authority_level"] == "regulation"
    assert document["evidence"][0]["section_id"] == "sec-004"
    assert document["evidence"][0]["heading_path"] == [
        "개인정보 처리 규정",
        "제4조 보관 기간",
    ]
    assert document["citations"] == ["[REG-000001 rev.1 §sec-004]"]
    assert payload["assistant_text"] == "관련 문서와 근거를 찾았습니다."
    assert document["citations"][0] not in payload["assistant_text"]
    assert response.headers["X-Request-ID"]


def test_api_root_redirects_to_self_contained_docs(client: TestClient) -> None:
    root = client.get("/", follow_redirects=False)
    assert root.status_code == 307
    assert root.headers["location"] == "/docs"

    docs = client.get("/docs")
    assert docs.status_code == 200
    assert docs.headers["cache-control"] == "no-store"
    assert "cdn.jsdelivr.net" not in docs.text
    assert "<script" not in docs.text
    assert "/api/v1/health" in docs.text
    assert "/api/v1/chat/messages" in docs.text
    assert "/openapi.json" in docs.text


def test_chat_returns_structured_not_found(client: TestClient) -> None:
    response = client.post(
        "/api/v1/chat/messages",
        json={"conversation_id": "conv_demo", "message": "양자역학 실험실 예약"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["response_type"] == "error"
    assert payload["error"]["code"] == "DOCUMENT_NOT_FOUND"


def test_delete_chat_conversation_removes_only_the_authenticated_owners_history(
    test_app: FastAPI,
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    state = test_app.state.container.state
    owners = (("demo-editor", "owned-message"), ("other-user", "other-message"))
    for subject_id, message_id in owners:
        state.record_message(
            conversation_id="conversation-to-delete",
            tenant_id="demo",
            subject_id=subject_id,
            message_id=message_id,
            role="user",
            content="keep scoped history private",
        )
    state.record_agent_run(
        run_id="owned-run",
        conversation_id="conversation-to-delete",
        tenant_id="demo",
        subject_id="demo-editor",
        status="succeeded",
        provider="claude",
        runtime_fingerprint="runtime-1",
        session_id="session-1",
    )

    response = client.delete(
        "/api/v2/chat/conversations/conversation-to-delete",
        headers=auth_headers,
    )
    replay = client.delete(
        "/api/v2/chat/conversations/conversation-to-delete",
        headers=auth_headers,
    )

    assert response.status_code == replay.status_code == 204
    assert state.recent_messages(
        conversation_id="conversation-to-delete",
        tenant_id="demo",
        subject_id="demo-editor",
    ) == []
    assert state.latest_agent_session(
        conversation_id="conversation-to-delete",
        tenant_id="demo",
        subject_id="demo-editor",
        runtime_fingerprint="runtime-1",
        max_age_seconds=3_600,
    ) is None
    assert state.recent_messages(
        conversation_id="conversation-to-delete",
        tenant_id="demo",
        subject_id="other-user",
    ) == [{"role": "user", "content": "keep scoped history private"}]


def test_delete_chat_conversation_requires_authentication(client: TestClient) -> None:
    response = client.delete("/api/v2/chat/conversations/conversation-to-delete")

    assert response.status_code == 401


def test_document_detail_returns_404_for_unknown_id(client: TestClient) -> None:
    response = client.get("/api/v1/documents/REG-999999")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "DOCUMENT_NOT_FOUND"


def test_document_detail_hides_restricted_document_from_anonymous_user(
    client: TestClient,
) -> None:
    response = client.get("/api/v1/documents/REP-000001")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "DOCUMENT_NOT_FOUND"


def test_invalid_bearer_token_is_not_downgraded_to_anonymous(client: TestClient) -> None:
    response = client.get(
        "/api/v1/documents/REG-000001",
        headers={"Authorization": "Bearer invalid-token"},
    )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "UNAUTHORIZED"


def test_chat_idempotency_replays_exact_response_and_rejects_new_body(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    headers = {**auth_headers, "Idempotency-Key": "chat-idempotency-key"}
    body = {
        "conversation_id": "idempotent-chat",
        "message": 'REG-000001에서 "1년"을 "3년"으로 변경해줘',
        "selected_document_id": "REG-000001",
    }

    first = client.post("/api/v1/chat/messages", headers=headers, json=body)
    replay = client.post("/api/v1/chat/messages", headers=headers, json=body)
    conflict = client.post(
        "/api/v1/chat/messages",
        headers=headers,
        json={**body, "message": "개인정보 문서 어디 있어?"},
    )

    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_request_size_guard_returns_413(client: TestClient) -> None:
    response = client.post(
        "/api/v1/chat/messages",
        content=b"x" * 1_300_000,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "REQUEST_TOO_LARGE"


def test_chunked_request_size_guard_returns_413(client: TestClient) -> None:
    response = client.post(
        "/api/v1/chat/messages",
        content=(chunk for chunk in [b" " * 650_000, b" " * 650_000, b"{}"]),
        headers={"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
    )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "REQUEST_TOO_LARGE"


def test_authorization_header_size_guard_returns_431(client: TestClient) -> None:
    response = client.get(
        "/api/v1/documents/REG-000001",
        headers={"Authorization": f"Bearer {'x' * 8_193}"},
    )

    assert response.status_code == 431
    assert response.json()["detail"]["code"] == "REQUEST_HEADER_TOO_LARGE"


def test_authenticated_requests_have_an_ip_rate_limit(
    test_app: FastAPI,
    auth_headers: dict[str, str],
) -> None:
    limited_settings = test_app.state.container.settings.model_copy(
        update={"auth_rate_limit_requests": 1}
    )
    from codegate_api.main import create_app

    limited_app = create_app(limited_settings)
    with TestClient(limited_app) as limited_client:
        first = limited_client.get(
            "/api/v1/documents/REG-000001",
            headers=auth_headers,
        )
        second = limited_client.get(
            "/api/v1/documents/REG-000001",
            headers=auth_headers,
        )

    assert first.status_code == 200
    assert second.status_code == 429


def test_rate_limit_is_enforced_per_caller(test_app: FastAPI) -> None:
    container = test_app.state.container
    limited_settings = container.settings.model_copy(
        update={"chat_rate_limit_requests": 2, "chat_rate_limit_window_seconds": 60}
    )
    from codegate_api.main import create_app

    limited_app = create_app(limited_settings)
    with TestClient(limited_app) as limited_client:
        for index in range(2):
            response = limited_client.post(
                "/api/v1/chat/messages",
                json={"conversation_id": f"rate-{index}", "message": "개인정보 문서"},
            )
            assert response.status_code == 200
        blocked = limited_client.post(
            "/api/v1/chat/messages",
            json={"conversation_id": "rate-blocked", "message": "개인정보 문서"},
        )

    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == "60"


def test_rate_limit_normalizes_equivalent_bearer_headers(
    test_app: FastAPI,
    auth_headers: dict[str, str],
) -> None:
    container = test_app.state.container
    limited_settings = container.settings.model_copy(
        update={"chat_rate_limit_requests": 1, "chat_rate_limit_window_seconds": 60}
    )
    from codegate_api.main import create_app

    token = auth_headers["Authorization"].removeprefix("Bearer ")
    limited_app = create_app(limited_settings)
    with TestClient(limited_app) as limited_client:
        first = limited_client.post(
            "/api/v1/chat/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"conversation_id": "rate-token-a", "message": "개인정보 문서"},
        )
        second = limited_client.post(
            "/api/v1/chat/messages",
            headers={"Authorization": f"bearer  {token}"},
            json={"conversation_id": "rate-token-b", "message": "개인정보 문서"},
        )

    assert first.status_code == 200
    assert second.status_code == 429
