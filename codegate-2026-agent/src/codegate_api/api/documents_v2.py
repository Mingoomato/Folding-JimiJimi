from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Response, status
from fastapi.responses import FileResponse

from codegate_api.container import AppContainer
from codegate_api.dependencies import get_access_context, get_container, get_knowledge_repository
from codegate_api.documents.models import (
    ApproveDocumentPlanRequest,
    CapabilityRegistryView,
    DocumentChatMessageResponse,
    DocumentCreationPlanRequest,
    DocumentExecutionView,
    DocumentPlanView,
    MutationPlanRequest,
    RejectDocumentPlanRequest,
    RejectDocumentPlanResponse,
)
from codegate_api.documents.service import DocumentService, DocumentServiceError
from codegate_api.knowledge.access import AccessContext
from codegate_api.knowledge.repository import KnowledgeRepository
from codegate_api.models import ChatMessageRequest
from codegate_api.state.store import StateConflict

router = APIRouter(tags=["document-editing-v2"])

IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=128),
]


def _service(container: AppContainer) -> DocumentService:
    return container.document_service


def _raise(error: DocumentServiceError) -> NoReturn:
    from fastapi import HTTPException

    raise HTTPException(
        status_code=error.status_code,
        detail={
            "code": error.code,
            "message": str(error),
            "retryable": error.retryable,
        },
    ) from error


@router.get("/document-capabilities", response_model=CapabilityRegistryView)
def get_document_capabilities(
    container: Annotated[AppContainer, Depends(get_container)],
) -> CapabilityRegistryView:
    return _service(container).capabilities()


@router.post("/chat/messages", response_model=DocumentChatMessageResponse)
async def create_document_chat_message(
    body: ChatMessageRequest,
    repository: Annotated[KnowledgeRepository, Depends(get_knowledge_repository)],
    access_context: Annotated[AccessContext, Depends(get_access_context)],
    container: Annotated[AppContainer, Depends(get_container)],
    idempotency_key: IdempotencyKey,
) -> DocumentChatMessageResponse:
    try:
        return await container.chat.create_document_message(
            request=body,
            repository=repository,
            access_context=access_context,
            idempotency_key=idempotency_key,
        )
    except StateConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": error.code, "message": str(error)},
        ) from error


@router.delete(
    "/chat/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_chat_conversation(
    conversation_id: Annotated[
        str,
        Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
    ],
    access_context: Annotated[AccessContext, Depends(get_access_context)],
    container: Annotated[AppContainer, Depends(get_container)],
) -> Response:
    if access_context.subject_id is None or access_context.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "로그인이 필요합니다."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    await container.chat.delete_conversation(
        conversation_id=conversation_id,
        access_context=access_context,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/documents/{document_id}/mutation-plans",
    response_model=DocumentPlanView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_mutation_plan(
    document_id: str,
    body: MutationPlanRequest,
    access_context: Annotated[AccessContext, Depends(get_access_context)],
    container: Annotated[AppContainer, Depends(get_container)],
    idempotency_key: IdempotencyKey,
    response: Response,
) -> DocumentPlanView:
    try:
        plan = _service(container).queue_mutation(
            document_id,
            body,
            access_context=access_context,
            idempotency_key=idempotency_key,
        )
    except DocumentServiceError as error:
        _raise(error)
    response.headers["Location"] = f"/api/v2/change-plans/{plan.change_plan_id}"
    return plan


@router.post(
    "/document-creation-plans",
    response_model=DocumentPlanView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_document_plan(
    body: DocumentCreationPlanRequest,
    access_context: Annotated[AccessContext, Depends(get_access_context)],
    container: Annotated[AppContainer, Depends(get_container)],
    idempotency_key: IdempotencyKey,
    response: Response,
) -> DocumentPlanView:
    try:
        plan = _service(container).queue_creation(
            body,
            access_context=access_context,
            idempotency_key=idempotency_key,
        )
    except DocumentServiceError as error:
        _raise(error)
    response.headers["Location"] = f"/api/v2/change-plans/{plan.change_plan_id}"
    return plan


@router.get("/change-plans/{plan_id}", response_model=DocumentPlanView)
def get_change_plan(
    plan_id: str,
    access_context: Annotated[AccessContext, Depends(get_access_context)],
    container: Annotated[AppContainer, Depends(get_container)],
) -> DocumentPlanView:
    try:
        return _service(container).get_plan(plan_id, access_context=access_context)
    except DocumentServiceError as error:
        _raise(error)


@router.get("/change-plans/{plan_id}/previews/{artifact_id}")
def get_change_preview(
    plan_id: str,
    artifact_id: str,
    access_context: Annotated[AccessContext, Depends(get_access_context)],
    container: Annotated[AppContainer, Depends(get_container)],
) -> FileResponse:
    try:
        path, media_type = _service(container).preview_path(
            plan_id,
            artifact_id,
            access_context=access_context,
        )
    except DocumentServiceError as error:
        _raise(error)
    return FileResponse(path, media_type=media_type, filename=path.name)


@router.post(
    "/change-plans/{plan_id}/approve",
    response_model=DocumentExecutionView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def approve_change_plan(
    plan_id: str,
    body: ApproveDocumentPlanRequest,
    access_context: Annotated[AccessContext, Depends(get_access_context)],
    container: Annotated[AppContainer, Depends(get_container)],
    idempotency_key: IdempotencyKey,
    response: Response,
) -> DocumentExecutionView:
    try:
        execution = await _service(container).approve(
            plan_id,
            body,
            access_context=access_context,
            idempotency_key=idempotency_key,
        )
    except DocumentServiceError as error:
        _raise(error)
    response.headers["Location"] = f"/api/v2/executions/{execution.execution_id}"
    return execution


@router.post("/change-plans/{plan_id}/reject", response_model=RejectDocumentPlanResponse)
def reject_change_plan(
    plan_id: str,
    body: RejectDocumentPlanRequest,
    access_context: Annotated[AccessContext, Depends(get_access_context)],
    container: Annotated[AppContainer, Depends(get_container)],
    idempotency_key: IdempotencyKey,
) -> RejectDocumentPlanResponse:
    try:
        return _service(container).reject(
            plan_id,
            body,
            access_context=access_context,
            idempotency_key=idempotency_key,
        )
    except DocumentServiceError as error:
        _raise(error)


@router.get("/executions/{execution_id}", response_model=DocumentExecutionView)
def get_execution(
    execution_id: str,
    access_context: Annotated[AccessContext, Depends(get_access_context)],
    container: Annotated[AppContainer, Depends(get_container)],
) -> DocumentExecutionView:
    try:
        return _service(container).get_execution(
            execution_id,
            access_context=access_context,
        )
    except DocumentServiceError as error:
        _raise(error)


@router.post(
    "/executions/{execution_id}/retry-sync",
    response_model=DocumentExecutionView,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_execution_sync(
    execution_id: str,
    access_context: Annotated[AccessContext, Depends(get_access_context)],
    container: Annotated[AppContainer, Depends(get_container)],
    idempotency_key: IdempotencyKey,
) -> DocumentExecutionView:
    try:
        return _service(container).retry_sync(
            execution_id,
            access_context=access_context,
            idempotency_key=idempotency_key,
        )
    except DocumentServiceError as error:
        _raise(error)


@router.post(
    "/executions/{execution_id}/undo",
    response_model=DocumentExecutionView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def undo_execution(
    execution_id: str,
    access_context: Annotated[AccessContext, Depends(get_access_context)],
    container: Annotated[AppContainer, Depends(get_container)],
    idempotency_key: IdempotencyKey,
    response: Response,
) -> DocumentExecutionView:
    try:
        execution = await _service(container).undo(
            execution_id,
            access_context=access_context,
            idempotency_key=idempotency_key,
        )
    except DocumentServiceError as error:
        _raise(error)
    response.headers["Location"] = f"/api/v2/executions/{execution.execution_id}"
    return execution
