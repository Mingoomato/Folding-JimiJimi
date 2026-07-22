import json
from functools import partial
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

from codegate_api.agent.gateway import AgentGatewayError, AgentOutcome, GeminiAgentGateway
from codegate_api.config import Settings
from codegate_api.container import build_container
from codegate_api.files.resolver import SourceUriResolver
from codegate_api.knowledge.access import AccessContext
from codegate_api.knowledge.repository import KnowledgeRepository
from codegate_api.knowledge.schemas import AccessLevel


def _repository() -> KnowledgeRepository:
    repository = KnowledgeRepository(
        Path("demo/llm-wiki"),
        SourceUriResolver(Path("demo/source")),
    )
    repository.load()
    return repository


def _local_access() -> AccessContext:
    return AccessContext(
        subject_id="local-user",
        tenant_id="local",
        readable_access=frozenset(
            {AccessLevel.PUBLIC, AccessLevel.INTERNAL, AccessLevel.RESTRICTED}
        ),
        writable_document_ids=frozenset(),
        provisioned=True,
        allow_all_writes=True,
        authz_source="local-workspace",
    )


def _gemini_response(text: str, *, finish_reason: str = "STOP") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "finishReason": finish_reason,
                    "content": {"parts": [{"text": text}]},
                }
            ]
        },
    )


def _decide(
    gateway: GeminiAgentGateway,
    *,
    message: str,
    selected_document_id: str | None = None,
    access_context: AccessContext | None = None,
) -> Any:
    return anyio.run(
        partial(
            gateway.decide,
            message=message,
            selected_document_id=selected_document_id,
            conversation_id="conversation-1",
            repository=_repository(),
            access_context=access_context or AccessContext.anonymous(),
        )
    )


def test_gemini_locate_uses_header_key_bounded_evidence_and_structured_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "test-gemini-secret"
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        captured["body"] = json.loads(request.content)
        return _gemini_response(
            json.dumps(
                {"assistant_text": "개인정보 보관 기간은 현재 근거상 1년입니다."},
                ensure_ascii=False,
            )
        )

    gateway = GeminiAgentGateway(
        Settings(agent_mode="gemini"),
        transport=httpx.MockTransport(handler),
        retry_delay_seconds=0,
    )
    result = _decide(gateway, message="개인정보 보관 기간은 얼마야?")

    assert result.provider == "gemini-rest"
    assert result.decision.outcome is AgentOutcome.READY
    assert result.decision.target_document_id == "REG-000001"
    assert result.decision.source_sha256 is None
    assert result.decision.operation is None
    assert result.tool_calls == ["mcp__codegate__knowledge_search"]
    request = captured["request"]
    assert request.headers["x-goog-api-key"] == secret
    assert secret not in str(request.url)
    assert request.url.path.endswith("/gemini-2.5-flash-lite:generateContent")
    body = captured["body"]
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    prompt = json.loads(body["contents"][0]["parts"][0]["text"])
    evidence = prompt["untrusted_input"]["evidence"]
    assert 1 <= len(evidence) <= 3
    assert all(len(item["quote"]) <= 1_800 for item in evidence)
    assert all(item["document_id"] != "REP-000001" for item in evidence)


def test_gemini_retries_only_transient_http_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    statuses = [429, 503]
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if statuses:
            return httpx.Response(statuses.pop(0), json={"error": {"message": "transient"}})
        return _gemini_response('{"assistant_text":"현재 근거를 확인했습니다."}')

    gateway = GeminiAgentGateway(
        Settings(agent_mode="gemini"),
        transport=httpx.MockTransport(handler),
        retry_delay_seconds=0,
    )

    result = _decide(gateway, message="개인정보 규정은 어디 있어?")

    assert result.decision.outcome is AgentOutcome.READY
    assert calls == 3


def test_gemini_does_not_retry_non_transient_or_expose_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "do-not-leak-this-key"
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, text=f"provider echoed {secret}")

    gateway = GeminiAgentGateway(
        Settings(agent_mode="gemini"),
        transport=httpx.MockTransport(handler),
        retry_delay_seconds=0,
    )

    with pytest.raises(AgentGatewayError) as caught:
        _decide(gateway, message="개인정보 규정은 어디 있어?")

    assert caught.value.code == "AGENT_UNAVAILABLE"
    assert secret not in str(caught.value)
    assert calls == 1


@pytest.mark.parametrize(
    "model_text",
    [
        "not-json",
        '{"assistant_text":"ok","source_sha256":"fabricated"}',
        '{"assistant_text":"   "}',
    ],
)
def test_gemini_fails_closed_on_invalid_structured_output(
    monkeypatch: pytest.MonkeyPatch,
    model_text: str,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    gateway = GeminiAgentGateway(
        Settings(agent_mode="gemini"),
        transport=httpx.MockTransport(lambda _: _gemini_response(model_text)),
    )

    with pytest.raises(AgentGatewayError) as caught:
        _decide(gateway, message="개인정보 규정은 어디 있어?")

    assert caught.value.code == "AGENT_RESULT_INVALID"


def test_gemini_change_is_deterministic_and_never_calls_model() -> None:
    def unexpected_call(_: httpx.Request) -> httpx.Response:
        raise AssertionError("a change request must not be sent to Gemini")

    gateway = GeminiAgentGateway(
        Settings(agent_mode="gemini"),
        transport=httpx.MockTransport(unexpected_call),
    )
    result = _decide(
        gateway,
        message='REG-000001에서 "1년"을 "3년"으로 변경',
        selected_document_id="REG-000001",
        access_context=_local_access(),
    )

    assert result.decision.outcome is AgentOutcome.READY
    assert result.decision.target_document_id == "REG-000001"
    assert result.decision.source_sha256 == (
        "7e2d690883e207dea2880bef301fc000ea6ee384c7e7e3415e5618872a614e26"
    )
    assert result.decision.operation is not None
    assert result.decision.operation.expected_text == "1년"
    assert result.decision.operation.replacement_text == "3년"


def test_gemini_change_fails_closed_for_ambiguous_source_text() -> None:
    gateway = GeminiAgentGateway(
        Settings(agent_mode="gemini"),
        transport=httpx.MockTransport(
            lambda _: (_ for _ in ()).throw(AssertionError("model must not be called"))
        ),
    )

    result = _decide(
        gateway,
        message='REG-000001에서 "개인정보"를 "정보"로 변경',
        selected_document_id="REG-000001",
        access_context=_local_access(),
    )

    assert result.decision.outcome is AgentOutcome.NEEDS_CLARIFICATION
    assert result.decision.source_sha256 is None
    assert result.decision.operation is None


def test_gemini_ambiguous_change_intent_never_calls_model() -> None:
    def unexpected_call(_: httpx.Request) -> httpx.Response:
        raise AssertionError("an ambiguous change request must not be sent to Gemini")

    gateway = GeminiAgentGateway(
        Settings(agent_mode="gemini"),
        transport=httpx.MockTransport(unexpected_call),
    )

    result = _decide(
        gateway,
        message="REG-000001의 보관 기간을 수정해줘",
        selected_document_id="REG-000001",
        access_context=_local_access(),
    )

    assert result.decision.intent == "change"
    assert result.decision.outcome is AgentOutcome.NEEDS_CLARIFICATION
    assert result.decision.source_sha256 is None
    assert result.decision.operation is None


def test_gemini_repository_lookup_enforces_access_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _gemini_response('{"assistant_text":"must not run"}')

    gateway = GeminiAgentGateway(
        Settings(agent_mode="gemini", gemini_data_policy="paid-no-training"),
        transport=httpx.MockTransport(handler),
    )

    result = _decide(
        gateway,
        message="REP-000001 문서를 보여줘",
        selected_document_id="REP-000001",
        access_context=AccessContext.anonymous(),
    )

    assert result.decision.outcome is AgentOutcome.NOT_FOUND
    assert result.decision.target_document_id is None
    assert calls == 0


def test_gemini_free_policy_blocks_non_public_evidence_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _gemini_response('{"assistant_text":"should not run"}')

    gateway = GeminiAgentGateway(
        Settings(agent_mode="gemini", gemini_data_policy="development-free"),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(AgentGatewayError) as caught:
        _decide(
            gateway,
            message="REP-000001 문서를 보여줘",
            selected_document_id="REP-000001",
            access_context=_local_access(),
        )

    assert caught.value.code == "GEMINI_DATA_POLICY_BLOCKED"
    assert calls == 0


def test_gemini_paid_policy_allows_non_public_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _gemini_response('{"assistant_text":"제한 문서 근거를 확인했습니다."}')

    gateway = GeminiAgentGateway(
        Settings(agent_mode="gemini", gemini_data_policy="paid-no-training"),
        transport=httpx.MockTransport(handler),
    )
    result = _decide(
        gateway,
        message="REP-000001 문서를 보여줘",
        selected_document_id="REP-000001",
        access_context=_local_access(),
    )

    assert result.decision.outcome is AgentOutcome.READY
    assert calls == 1


def test_gemini_container_health_and_startup_require_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    container = build_container(Settings(agent_mode="gemini"))

    assert container.agent_available is False
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        anyio.run(container.startup)

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    assert container.agent_available is True
