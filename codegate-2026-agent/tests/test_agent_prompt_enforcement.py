import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session

from codegate_api.agent.gateway import (
    AgentDecision,
    AgentGatewayError,
    AgentOutcome,
    AgentRunResult,
    ClaudeAgentGateway,
)
from codegate_api.agent.tools import create_codegate_tool_server
from codegate_api.config import Settings
from codegate_api.files.resolver import SourceUriResolver
from codegate_api.knowledge.access import AccessContext
from codegate_api.knowledge.repository import KnowledgeRepository
from codegate_api.knowledge.schemas import AccessLevel
from codegate_api.state.store import StateStore


class StubAgent:
    def __init__(self, decision: AgentDecision) -> None:
        self._decision = decision

    @property
    def session_ttl_seconds(self) -> None:
        return None

    def session_fingerprint(self, **_: Any) -> None:
        return None

    async def decide(self, **_: Any) -> AgentRunResult:
        return AgentRunResult(decision=self._decision, provider="stub")


class SessionStubAgent:
    def __init__(self, decision: AgentDecision) -> None:
        self._decision = decision
        self.resume_attempts: list[str | None] = []

    @property
    def session_ttl_seconds(self) -> int:
        return 3_600

    def session_fingerprint(self, **_: Any) -> str:
        return "a" * 64

    async def decide(self, **kwargs: Any) -> AgentRunResult:
        self.resume_attempts.append(kwargs.get("resume_session_id"))
        if len(self.resume_attempts) == 2:
            raise AgentGatewayError("AGENT_TIMEOUT", "timed out")
        return AgentRunResult(
            decision=self._decision,
            provider="stub",
            session_id="11111111-1111-4111-8111-111111111111",
        )


def _decision(
    *,
    outcome: AgentOutcome,
    intent: str,
    target_document_id: str | None = None,
    source_sha256: str | None = None,
    expected_text: str | None = None,
    replacement_text: str | None = None,
    search_query: str = "개인정보 보관 기간",
) -> AgentDecision:
    operation = None
    if expected_text is not None and replacement_text is not None:
        operation = {
            "type": "replace_exact",
            "expected_text": expected_text,
            "replacement_text": replacement_text,
            "expected_occurrences": 1,
        }
    return AgentDecision.model_validate(
        {
            "prompt_contract_version": "2.0",
            "outcome": outcome,
            "intent": intent,
            "assistant_text": "검증 상태를 확인했습니다.",
            "search_query": search_query,
            "target_document_id": target_document_id,
            "source_sha256": source_sha256,
            "operation": operation,
        }
    )


def _install_stub(test_app: FastAPI, decision: AgentDecision) -> None:
    test_app.state.container.chat._agent = StubAgent(decision)  # noqa: SLF001


@pytest.mark.parametrize(
    ("outcome", "intent", "expected_code", "retryable"),
    [
        (AgentOutcome.NEEDS_CLARIFICATION, "change", "CHANGE_DETAILS_REQUIRED", False),
        (AgentOutcome.NOT_FOUND, "locate", "DOCUMENT_NOT_FOUND", False),
        (AgentOutcome.TOOL_ERROR, "locate", "AGENT_TOOL_ERROR", True),
        (AgentOutcome.UNSUPPORTED, "locate", "UNSUPPORTED_AGENT_REQUEST", False),
    ],
)
def test_non_ready_agent_outcomes_stop_before_successful_search(
    client: TestClient,
    test_app: FastAPI,
    auth_headers: dict[str, str],
    outcome: AgentOutcome,
    intent: str,
    expected_code: str,
    retryable: bool,
) -> None:
    _install_stub(test_app, _decision(outcome=outcome, intent=intent))

    response = client.post(
        "/api/v1/chat/messages",
        headers=auth_headers,
        json={
            "conversation_id": f"outcome-{outcome.value}",
            "message": "개인정보 보관 기간을 찾아줘",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["response_type"] == "error"
    assert body["documents"] == []
    assert body["error"] == {
        "code": expected_code,
        "message": body["assistant_text"],
        "retryable": retryable,
    }


def test_chat_service_resumes_only_after_the_latest_successful_sdk_run(
    client: TestClient,
    test_app: FastAPI,
    auth_headers: dict[str, str],
) -> None:
    agent = SessionStubAgent(_decision(outcome=AgentOutcome.UNSUPPORTED, intent="locate"))
    test_app.state.container.chat._agent = agent  # noqa: SLF001

    for index in range(3):
        response = client.post(
            "/api/v1/chat/messages",
            headers=auth_headers,
            json={
                "conversation_id": "session-rotation",
                "message": f"unsafe request {index}",
            },
        )
        assert response.status_code == 200

    assert agent.resume_attempts == [
        None,
        "11111111-1111-4111-8111-111111111111",
        None,
    ]


def test_change_preview_rejects_selected_and_verified_target_mismatch(
    client: TestClient,
    test_app: FastAPI,
    auth_headers: dict[str, str],
) -> None:
    _install_stub(
        test_app,
        _decision(
            outcome=AgentOutcome.READY,
            intent="change",
            target_document_id="REG-000002",
            source_sha256=("b91943f04a4bb2631e357fdc36de67b4b4e2b30bca58ccb279192d913e8bec01"),
            expected_text="분기마다",
            replacement_text="매월",
            search_query="REG-000002 접근 권한",
        ),
    )

    response = client.post(
        "/api/v1/chat/messages",
        headers=auth_headers,
        json={
            "conversation_id": "selected-mismatch",
            "message": 'REG-000002에서 "분기마다"를 "매월"로 변경해줘',
            "selected_document_id": "REG-000001",
        },
    )

    assert response.json()["error"]["code"] == "SELECTED_DOCUMENT_MISMATCH"
    assert response.json()["change_plan"] is None


def test_change_preview_binds_decision_to_current_source_hash(
    client: TestClient,
    test_app: FastAPI,
    auth_headers: dict[str, str],
) -> None:
    _install_stub(
        test_app,
        _decision(
            outcome=AgentOutcome.READY,
            intent="change",
            target_document_id="REG-000001",
            source_sha256="f" * 64,
            expected_text="1년",
            replacement_text="3년",
        ),
    )

    response = client.post(
        "/api/v1/chat/messages",
        headers=auth_headers,
        json={
            "conversation_id": "source-hash-binding",
            "message": 'REG-000001에서 "1년"을 "3년"으로 변경해줘',
            "selected_document_id": "REG-000001",
        },
    )

    assert response.json()["error"]["code"] == "SOURCE_HASH_CONFLICT"
    assert response.json()["change_plan"] is None


def test_change_preview_rejects_replacement_not_present_in_user_request(
    client: TestClient,
    test_app: FastAPI,
    auth_headers: dict[str, str],
) -> None:
    _install_stub(
        test_app,
        _decision(
            outcome=AgentOutcome.READY,
            intent="change",
            target_document_id="REG-000001",
            source_sha256=("7e2d690883e207dea2880bef301fc000ea6ee384c7e7e3415e5618872a614e26"),
            expected_text="1년",
            replacement_text="5년",
        ),
    )

    response = client.post(
        "/api/v1/chat/messages",
        headers=auth_headers,
        json={
            "conversation_id": "replacement-grounding",
            "message": 'REG-000001에서 "1년"을 "3년"으로 변경해줘',
            "selected_document_id": "REG-000001",
        },
    )

    assert response.json()["error"]["code"] == "CHANGE_REPLACEMENT_UNVERIFIED"
    assert response.json()["change_plan"] is None


def _repository() -> KnowledgeRepository:
    repository = KnowledgeRepository(
        Path("demo/llm-wiki"),
        SourceUriResolver(Path("demo/source")),
    )
    repository.load()
    return repository


def test_session_fingerprint_rotates_for_model_and_authorization_changes() -> None:
    repository = _repository()
    anonymous = AccessContext.anonymous()
    editor = AccessContext(
        subject_id="user-1",
        tenant_id="tenant-1",
        readable_access=frozenset({AccessLevel.PUBLIC}),
        writable_document_ids=frozenset({"REG-000001"}),
        provisioned=True,
        authz_source="test",
    )
    first = ClaudeAgentGateway(Settings(agent_mode="claude", claude_model="model-a"))
    second = ClaudeAgentGateway(Settings(agent_mode="claude", claude_model="model-b"))

    fingerprint = first.session_fingerprint(repository=repository, access_context=anonymous)

    assert len(fingerprint) == 64
    assert fingerprint == first.session_fingerprint(
        repository=repository,
        access_context=anonymous,
    )
    assert fingerprint != first.session_fingerprint(
        repository=repository,
        access_context=editor,
    )
    assert fingerprint != second.session_fingerprint(
        repository=repository,
        access_context=anonymous,
    )


def test_state_store_resumes_only_matching_runtime_fingerprint(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    store.record_message(
        conversation_id="conversation-1",
        tenant_id="tenant-1",
        subject_id="user-1",
        message_id="message-1",
        role="user",
        content="문서를 찾아줘",
    )
    store.record_agent_run(
        run_id="run-1",
        conversation_id="conversation-1",
        tenant_id="tenant-1",
        subject_id="user-1",
        status="succeeded",
        provider="claude-agent-sdk",
        runtime_fingerprint="a" * 64,
        session_id="11111111-1111-4111-8111-111111111111",
    )

    assert (
        store.latest_agent_session(
            conversation_id="conversation-1",
            tenant_id="tenant-1",
            subject_id="user-1",
            runtime_fingerprint="a" * 64,
            max_age_seconds=3_600,
        )
        == "11111111-1111-4111-8111-111111111111"
    )
    assert (
        store.latest_agent_session(
            conversation_id="conversation-1",
            tenant_id="tenant-1",
            subject_id="user-1",
            runtime_fingerprint="b" * 64,
            max_age_seconds=3_600,
        )
        is None
    )


@pytest.mark.parametrize("status", ["failed", "running"])
def test_state_store_rotates_session_after_non_successful_latest_run(
    tmp_path: Path,
    status: str,
) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    store.record_message(
        conversation_id="conversation-1",
        tenant_id="tenant-1",
        subject_id="user-1",
        message_id="message-1",
        role="user",
        content="문서를 찾아줘",
    )
    store.record_agent_run(
        run_id="run-1",
        conversation_id="conversation-1",
        tenant_id="tenant-1",
        subject_id="user-1",
        status="succeeded",
        provider="claude-agent-sdk",
        runtime_fingerprint="a" * 64,
        session_id="11111111-1111-4111-8111-111111111111",
    )
    store.record_agent_run(
        run_id="run-2",
        conversation_id="conversation-1",
        tenant_id="tenant-1",
        subject_id="user-1",
        status=status,
        provider="claude-agent-sdk",
        runtime_fingerprint="a" * 64,
        error_code="AGENT_TIMEOUT" if status == "failed" else None,
    )

    assert (
        store.latest_agent_session(
            conversation_id="conversation-1",
            tenant_id="tenant-1",
            subject_id="user-1",
            runtime_fingerprint="a" * 64,
            max_age_seconds=3_600,
        )
        is None
    )
    assert (
        store.latest_agent_session(
            conversation_id="conversation-1",
            tenant_id="tenant-1",
            subject_id="user-1",
            runtime_fingerprint="a" * 64,
            max_age_seconds=0,
        )
        is None
    )


def test_mcp_tools_expose_detailed_read_only_contract_and_untrusted_provenance() -> None:
    async def scenario() -> tuple[Any, Any]:
        repository = _repository()
        server = create_codegate_tool_server(repository, AccessContext.anonymous())
        async with create_connected_server_and_client_session(server["instance"]) as session:
            listed = await session.list_tools()
            source = await session.call_tool(
                "source_file_read",
                {"document_id": "REG-000001"},
            )
            return listed, source

    listed, source = anyio.run(scenario)

    assert {tool.name for tool in listed.tools} == {
        "knowledge_search",
        "document_get",
        "knowledge_document_read",
        "source_file_read",
    }
    for tool in listed.tools:
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.openWorldHint is False
        assert len(tool.description.split(".")) >= 4
        for definition in tool.inputSchema["properties"].values():
            assert definition["description"]

    payload = json.loads(source.content[0].text)
    assert source.isError is False
    assert payload["tool_contract_version"] == "2.0"
    assert payload["provenance"] == {
        "trust": "untrusted_content",
        "source_kind": "current_source",
        "graph_version": "2026-07-21.2-demo",
    }
    assert payload["source_sha256"] == (
        "7e2d690883e207dea2880bef301fc000ea6ee384c7e7e3415e5618872a614e26"
    )
