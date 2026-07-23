from __future__ import annotations

import asyncio
import hashlib
from uuid import uuid4

from codegate_api.agent.gateway import (
    AgentGateway,
    AgentGatewayError,
    AgentOutcome,
    is_report_draft_request,
)
from codegate_api.agent.instructions import ConversationTurn
from codegate_api.changes.hashing import request_hash
from codegate_api.changes.service import ChangePlanService, ChangeServiceError
from codegate_api.documents.models import (
    DocumentChatMessageResponse,
    DocumentOperation,
    MutationPlanRequest,
    PdfAnnotationAddOperation,
    PdfFormFieldSetOperation,
    PdfRedactTextOperation,
    SpreadsheetCellsSetOperation,
    TableCellSetOperation,
    TextReplaceOperation,
)
from codegate_api.documents.service import DocumentService, DocumentServiceError
from codegate_api.knowledge.access import AccessContext
from codegate_api.knowledge.repository import KnowledgeRepository
from codegate_api.models import (
    ApiError,
    ChatMessageRequest,
    ChatMessageResponse,
    DocumentResult,
    ResponseType,
)
from codegate_api.state.store import StateStore

MAX_LOCATION_DOCUMENTS = 1
MAX_LOCATION_ANSWER_CHARS = 480
MAX_REPORT_ANSWER_CHARS = 4_000
MAX_CONVERSATION_HISTORY_MESSAGES = 20
MAX_CONVERSATION_HISTORY_CHARS = 16_000


def _bounded_conversation_history(messages: list[dict[str, str]]) -> list[ConversationTurn]:
    selected: list[ConversationTurn] = []
    remaining = MAX_CONVERSATION_HISTORY_CHARS
    for message in reversed(messages):
        role = message.get("role")
        content = message.get("content", "").strip()
        if role not in {"user", "assistant"} or not content or remaining <= 0:
            continue
        bounded = content[-remaining:]
        selected.append(
            ConversationTurn(
                role="user" if role == "user" else "assistant",
                content=bounded,
            )
        )
        remaining -= len(bounded)
    selected.reverse()
    return selected


class ChatService:
    def __init__(
        self,
        *,
        agent: AgentGateway,
        plans: ChangePlanService,
        document_plans: DocumentService,
        state: StateStore,
    ) -> None:
        self._agent = agent
        self._plans = plans
        self._document_plans = document_plans
        self._state = state
        self._conversation_locks: dict[str, asyncio.Lock] = {}
        self._lock_guard = asyncio.Lock()

    async def delete_conversation(
        self,
        *,
        conversation_id: str,
        access_context: AccessContext,
    ) -> None:
        tenant_id = access_context.tenant_id or "public"
        subject_id = access_context.subject_id or "anonymous"
        owner_key = f"{tenant_id}:{subject_id}:{conversation_id}"
        async with self._lock_guard:
            conversation_lock = self._conversation_locks.setdefault(owner_key, asyncio.Lock())
        async with conversation_lock:
            self._state.delete_conversation(
                conversation_id=conversation_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
            )
        async with self._lock_guard:
            if not conversation_lock.locked():
                self._conversation_locks.pop(owner_key, None)

    async def create_message(
        self,
        *,
        request: ChatMessageRequest,
        repository: KnowledgeRepository,
        access_context: AccessContext,
        idempotency_key: str | None = None,
    ) -> ChatMessageResponse:
        owner_key = (
            f"{access_context.tenant_id or 'public'}:"
            f"{access_context.subject_id or 'anonymous'}:{request.conversation_id}"
        )
        async with self._lock_guard:
            conversation_lock = self._conversation_locks.setdefault(owner_key, asyncio.Lock())
        async with conversation_lock:
            tenant_id = access_context.tenant_id or "public"
            subject_id = access_context.subject_id or "anonymous"
            digest = request_hash(request.model_dump(mode="json"))
            if idempotency_key:
                replay = self._state.get_chat_idempotency(
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    key=idempotency_key,
                    request_hash=digest,
                )
                if replay is not None:
                    return ChatMessageResponse.model_validate_json(replay)
            response = await self._create_serialized(
                request=request,
                repository=repository,
                access_context=access_context,
                document_mode=False,
                document_plan_key=idempotency_key,
            )
            assert isinstance(response, ChatMessageResponse)
            if idempotency_key:
                self._state.put_chat_idempotency(
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    key=idempotency_key,
                    request_hash=digest,
                    response_json=response.model_dump_json(),
                )
            return response

    async def create_document_message(
        self,
        *,
        request: ChatMessageRequest,
        repository: KnowledgeRepository,
        access_context: AccessContext,
        idempotency_key: str,
    ) -> DocumentChatMessageResponse:
        owner_key = (
            f"{access_context.tenant_id or 'public'}:"
            f"{access_context.subject_id or 'anonymous'}:{request.conversation_id}"
        )
        async with self._lock_guard:
            conversation_lock = self._conversation_locks.setdefault(owner_key, asyncio.Lock())
        async with conversation_lock:
            tenant_id = access_context.tenant_id or "public"
            subject_id = access_context.subject_id or "anonymous"
            digest = request_hash(request.model_dump(mode="json"))
            state_key = hashlib.sha256(f"v2:{idempotency_key}".encode()).hexdigest()
            replay = self._state.get_chat_idempotency(
                tenant_id=tenant_id,
                subject_id=subject_id,
                key=state_key,
                request_hash=digest,
            )
            if replay is not None:
                return DocumentChatMessageResponse.model_validate_json(replay)
            response = await self._create_serialized(
                request=request,
                repository=repository,
                access_context=access_context,
                document_mode=True,
                document_plan_key=idempotency_key,
            )
            if isinstance(response, ChatMessageResponse):
                response = _document_chat_response(response)
            self._state.put_chat_idempotency(
                tenant_id=tenant_id,
                subject_id=subject_id,
                key=state_key,
                request_hash=digest,
                response_json=response.model_dump_json(),
            )
            return response

    async def _create_serialized(
        self,
        *,
        request: ChatMessageRequest,
        repository: KnowledgeRepository,
        access_context: AccessContext,
        document_mode: bool,
        document_plan_key: str | None,
    ) -> ChatMessageResponse | DocumentChatMessageResponse:
        tenant_id = access_context.tenant_id or "public"
        subject_id = access_context.subject_id or "anonymous"
        user_message_id = f"msg_{uuid4().hex}"
        assistant_message_id = f"msg_{uuid4().hex}"
        run_id = f"run_{uuid4().hex}"
        authenticated = access_context.subject_id is not None
        runtime_fingerprint = self._agent.session_fingerprint(
            repository=repository,
            access_context=access_context,
        )
        conversation_history = _bounded_conversation_history(
            self._state.recent_messages(
                conversation_id=request.conversation_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                limit=MAX_CONVERSATION_HISTORY_MESSAGES,
            )
        )
        resume_session_id = (
            self._state.latest_agent_session(
                conversation_id=request.conversation_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                runtime_fingerprint=runtime_fingerprint,
                max_age_seconds=self._agent.session_ttl_seconds,
            )
            if (
                authenticated
                and runtime_fingerprint is not None
                and self._agent.session_ttl_seconds is not None
            )
            else None
        )
        if authenticated:
            self._state.record_message(
                conversation_id=request.conversation_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                message_id=user_message_id,
                role="user",
                content=request.message,
            )
            self._state.record_agent_run(
                run_id=run_id,
                conversation_id=request.conversation_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                status="running",
                provider=type(self._agent).__name__,
                runtime_fingerprint=runtime_fingerprint,
            )
        try:
            result = await self._agent.decide(
                message=request.message,
                selected_document_id=request.selected_document_id,
                conversation_id=request.conversation_id,
                repository=repository,
                access_context=access_context,
                resume_session_id=resume_session_id,
                conversation_history=conversation_history,
            )
        except AgentGatewayError as error:
            if authenticated:
                self._state.record_agent_run(
                    run_id=run_id,
                    conversation_id=request.conversation_id,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    status="failed",
                    provider=type(self._agent).__name__,
                    runtime_fingerprint=runtime_fingerprint,
                    error_code=error.code,
                )
            response = _error_response(
                request.conversation_id,
                assistant_message_id,
                code=error.code,
                message=str(error),
            )
            self._record_assistant(response, tenant_id, subject_id)
            return response

        if authenticated:
            self._state.record_agent_run(
                run_id=run_id,
                conversation_id=request.conversation_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                status="succeeded",
                provider=result.provider,
                runtime_fingerprint=runtime_fingerprint,
                session_id=result.session_id,
            )
        decision = result.decision
        if decision.outcome is not AgentOutcome.READY:
            response = _agent_outcome_response(
                request.conversation_id,
                assistant_message_id,
                outcome=decision.outcome,
                intent=decision.intent,
            )
            self._record_assistant(response, tenant_id, subject_id)
            return response
        documents = repository.search(
            decision.search_query,
            access_context=access_context,
        )
        if decision.intent == "locate":
            response = self._location_response(
                request=request,
                message_id=assistant_message_id,
                repository=repository,
                access_context=access_context,
                documents=documents,
                assistant_text=decision.assistant_text,
                target_document_id=decision.target_document_id,
            )
            self._record_assistant(response, tenant_id, subject_id)
            return response

        if decision.operation is None and decision.document_operation is None:
            response = _error_response(
                request.conversation_id,
                assistant_message_id,
                code="CHANGE_DETAILS_REQUIRED",
                message="변경 전 텍스트와 변경 후 텍스트를 명확히 적어주세요.",
            )
            self._record_assistant(response, tenant_id, subject_id)
            return response
        if (
            decision.operation is not None
            and decision.operation.replacement_text not in request.message
        ):
            response = _error_response(
                request.conversation_id,
                assistant_message_id,
                code="CHANGE_REPLACEMENT_UNVERIFIED",
                message="변경 후 텍스트는 요청에 정확히 포함해 주세요.",
            )
            self._record_assistant(response, tenant_id, subject_id)
            return response
        if decision.document_operation is not None and not _native_values_explicit(
            decision.document_operation,
            request.message,
        ):
            response = _error_response(
                request.conversation_id,
                assistant_message_id,
                code="CHANGE_REPLACEMENT_UNVERIFIED",
                message="Native document replacement values must be explicit in the request.",
            )
            self._record_assistant(response, tenant_id, subject_id)
            return response
        if access_context.subject_id is None:
            response = _error_response(
                request.conversation_id,
                assistant_message_id,
                code="AUTHENTICATION_REQUIRED",
                message="문서 변경 미리보기는 로그인이 필요합니다.",
            )
            self._record_assistant(response, tenant_id, subject_id)
            return response

        if (
            request.selected_document_id is not None
            and decision.target_document_id != request.selected_document_id
        ):
            response = _error_response(
                request.conversation_id,
                assistant_message_id,
                code="SELECTED_DOCUMENT_MISMATCH",
                message=(
                    "선택한 문서와 요청에서 확인된 대상이 다릅니다. "
                    "수정할 문서를 다시 선택해 주세요."
                ),
            )
            self._record_assistant(response, tenant_id, subject_id)
            return response

        target_id = decision.target_document_id
        writable_candidates = [document for document in documents if document.can_write]
        if target_id is None:
            if len(writable_candidates) != 1:
                response = ChatMessageResponse(
                    conversation_id=request.conversation_id,
                    message_id=assistant_message_id,
                    response_type=ResponseType.TARGET_SELECTION,
                    assistant_text="수정할 문서를 선택해주세요.",
                    documents=writable_candidates,
                )
                self._record_assistant(response, tenant_id, subject_id)
                return response
            target_id = writable_candidates[0].document_id
        elif request.selected_document_id is None and target_id not in {
            document.document_id for document in writable_candidates
        }:
            response = _error_response(
                request.conversation_id,
                assistant_message_id,
                code="DOCUMENT_NOT_FOUND",
                message="검색 근거 안에서 수정 가능한 대상을 찾지 못했습니다.",
            )
            self._record_assistant(response, tenant_id, subject_id)
            return response

        if decision.document_operation is not None:
            if not document_mode or document_plan_key is None:
                response = _error_response(
                    request.conversation_id,
                    assistant_message_id,
                    code="NATIVE_DOCUMENT_V2_REQUIRED",
                    message="Native document changes require the /api/v2 chat contract.",
                )
                self._record_assistant(response, tenant_id, subject_id)
                return response
            assert decision.capability_id is not None
            assert decision.capability_snapshot_id is not None
            assert decision.graph_version is not None
            assert decision.source_sha256 is not None
            try:
                document_plan = self._document_plans.queue_mutation(
                    target_id,
                    MutationPlanRequest(
                        capability_id=decision.capability_id,
                        expected_source_sha256=decision.source_sha256,
                        capability_snapshot_id=decision.capability_snapshot_id,
                        graph_version=decision.graph_version,
                        operations=[decision.document_operation],
                    ),
                    access_context=access_context,
                    idempotency_key=document_plan_key,
                )
            except DocumentServiceError as error:
                response = _error_response(
                    request.conversation_id,
                    assistant_message_id,
                    code=error.code,
                    message=str(error),
                )
                self._record_assistant(response, tenant_id, subject_id)
                return response
            document = repository.get(target_id, access_context=access_context)
            response_v2 = DocumentChatMessageResponse(
                conversation_id=request.conversation_id,
                message_id=assistant_message_id,
                response_type=ResponseType.CHANGE_PREVIEW,
                assistant_text=(
                    "Native document preview is being prepared. Approve only the exact plan hash."
                ),
                documents=[document] if document is not None else [],
                document_plan=document_plan,
            )
            self._record_assistant(response_v2, tenant_id, subject_id)
            return response_v2

        assert decision.operation is not None
        try:
            plan = self._plans.create_plan(
                repository=repository,
                access_context=access_context,
                document_id=target_id,
                operation=decision.operation,
                request_text=request.message,
                expected_base_sha256=decision.source_sha256,
            )
        except ChangeServiceError as error:
            response = _error_response(
                request.conversation_id,
                assistant_message_id,
                code=error.code,
                message=str(error),
            )
            self._record_assistant(response, tenant_id, subject_id)
            return response
        document = repository.get(target_id, access_context=access_context)
        response = ChatMessageResponse(
            conversation_id=request.conversation_id,
            message_id=assistant_message_id,
            response_type=ResponseType.CHANGE_PREVIEW,
            assistant_text="아래 diff를 확인한 뒤 정확한 plan_hash로 승인해주세요.",
            documents=[document] if document is not None else [],
            change_plan=plan,
        )
        self._record_assistant(response, tenant_id, subject_id)
        return response

    def _location_response(
        self,
        *,
        request: ChatMessageRequest,
        message_id: str,
        repository: KnowledgeRepository,
        access_context: AccessContext,
        documents: list[DocumentResult],
        assistant_text: str,
        target_document_id: str | None,
    ) -> ChatMessageResponse:
        if target_document_id:
            target = repository.get(target_document_id, access_context=access_context)
            if target is not None:
                documents = [
                    target,
                    *[item for item in documents if item.document_id != target_document_id],
                ]
        if not documents:
            return _error_response(
                request.conversation_id,
                message_id,
                code="DOCUMENT_NOT_FOUND",
                message="검색어와 일치하는 근거 청크가 없습니다.",
            )
        displayed_documents = [
            document.model_copy(
                update={
                    "evidence": document.evidence[:1],
                    "citations": document.citations[:1],
                }
            )
            for document in documents[:MAX_LOCATION_DOCUMENTS]
            if document.evidence and document.citations
        ]
        return ChatMessageResponse(
            conversation_id=request.conversation_id,
            message_id=message_id,
            response_type=ResponseType.LOCATION_RESULT,
            assistant_text=(
                _report_answer(assistant_text)
                if is_report_draft_request(request.message)
                else _compact_answer(assistant_text)
            ),
            documents=displayed_documents,
        )

    def _record_assistant(
        self,
        response: ChatMessageResponse | DocumentChatMessageResponse,
        tenant_id: str,
        subject_id: str,
    ) -> None:
        if tenant_id == "public" and subject_id == "anonymous":
            return
        self._state.record_message(
            conversation_id=response.conversation_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
            message_id=response.message_id,
            role="assistant",
            content=response.assistant_text,
            response_type=response.response_type.value,
            metadata={
                "document_ids": [document.document_id for document in response.documents],
                "change_plan_id": (
                    response.change_plan.change_plan_id
                    if response.change_plan is not None
                    else None
                ),
                "document_plan_id": (
                    response.document_plan.change_plan_id
                    if isinstance(response, DocumentChatMessageResponse)
                    and response.document_plan is not None
                    else None
                ),
                "error_code": response.error.code if response.error is not None else None,
            },
        )


def _document_chat_response(response: ChatMessageResponse) -> DocumentChatMessageResponse:
    return DocumentChatMessageResponse(
        conversation_id=response.conversation_id,
        message_id=response.message_id,
        response_type=response.response_type,
        assistant_text=response.assistant_text,
        documents=response.documents,
        change_plan=response.change_plan,
        error=response.error,
    )


def _native_values_explicit(operation: DocumentOperation, message: str) -> bool:
    if isinstance(operation, TextReplaceOperation | TableCellSetOperation):
        return operation.replacement in message
    if isinstance(operation, PdfAnnotationAddOperation):
        return operation.text in message
    if isinstance(operation, PdfFormFieldSetOperation):
        return operation.replacement in message
    if isinstance(operation, PdfRedactTextOperation):
        return operation.expected in message
    if isinstance(operation, SpreadsheetCellsSetOperation):
        return all(
            _typed_value_explicit(cell.replacement.value, message) for cell in operation.cells
        )
    return False


def _typed_value_explicit(value: object, message: str) -> bool:
    if value is None:
        return any(marker in message.casefold() for marker in ("null", "empty", "비우", "삭제"))
    if isinstance(value, bool):
        markers = ("true", "참", "예") if value else ("false", "거짓", "아니오")
        return any(marker in message.casefold() for marker in markers)
    if isinstance(value, float) and value.is_integer():
        return str(int(value)) in message or str(value) in message
    return str(value) in message


def _error_response(
    conversation_id: str,
    message_id: str,
    *,
    code: str,
    message: str,
    retryable: bool = False,
) -> ChatMessageResponse:
    return ChatMessageResponse(
        conversation_id=conversation_id,
        message_id=message_id,
        response_type=ResponseType.ERROR,
        assistant_text=message,
        documents=[],
        error=ApiError(code=code, message=message, retryable=retryable),
    )


def _agent_outcome_response(
    conversation_id: str,
    message_id: str,
    *,
    outcome: AgentOutcome,
    intent: str,
) -> ChatMessageResponse:
    if outcome is AgentOutcome.NEEDS_CLARIFICATION:
        if intent == "change":
            return _error_response(
                conversation_id,
                message_id,
                code="CHANGE_DETAILS_REQUIRED",
                message="수정할 문서와 변경 전후 텍스트를 명확히 적어주세요.",
            )
        return _error_response(
            conversation_id,
            message_id,
            code="CLARIFICATION_REQUIRED",
            message="찾을 문서나 내용을 조금 더 구체적으로 적어주세요.",
        )
    if outcome is AgentOutcome.NOT_FOUND:
        return _error_response(
            conversation_id,
            message_id,
            code="DOCUMENT_NOT_FOUND",
            message="검색어와 일치하는 검증된 문서 근거가 없습니다.",
        )
    if outcome is AgentOutcome.TOOL_ERROR:
        return _error_response(
            conversation_id,
            message_id,
            code="AGENT_TOOL_ERROR",
            message="현재 문서 근거를 안전하게 확인하지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
            retryable=True,
        )
    return _error_response(
        conversation_id,
        message_id,
        code="UNSUPPORTED_AGENT_REQUEST",
        message="요청에서 처리할 수 있는 안전한 문서 작업을 확인하지 못했습니다.",
    )


def _compact_answer(value: str) -> str:
    answer = " ".join(value.split())
    if len(answer) <= MAX_LOCATION_ANSWER_CHARS:
        return answer
    candidate = answer[:MAX_LOCATION_ANSWER_CHARS]
    sentence_end = max(candidate.rfind("다."), candidate.rfind("요."), candidate.rfind("."))
    if sentence_end >= MAX_LOCATION_ANSWER_CHARS // 3:
        return candidate[: sentence_end + 1].rstrip()
    return candidate.rstrip() + "…"


def _report_answer(value: str) -> str:
    answer = value.strip()
    if len(answer) <= MAX_REPORT_ANSWER_CHARS:
        return answer
    return answer[:MAX_REPORT_ANSWER_CHARS].rstrip() + "…"
