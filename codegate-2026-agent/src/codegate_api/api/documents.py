from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status

from codegate_api.api.errors import change_http_error
from codegate_api.changes.service import ChangeServiceError
from codegate_api.container import AppContainer
from codegate_api.dependencies import get_access_context, get_container, get_knowledge_repository
from codegate_api.knowledge.access import AccessContext
from codegate_api.knowledge.repository import KnowledgeRepository
from codegate_api.models import ChangePlanView, DocumentResult, SourceSyncRequest

router = APIRouter(tags=["documents"])


@router.get("/documents/{document_id}", response_model=DocumentResult)
def get_document(
    document_id: str,
    repository: Annotated[KnowledgeRepository, Depends(get_knowledge_repository)],
    access_context: Annotated[AccessContext, Depends(get_access_context)],
) -> DocumentResult:
    document = repository.get(document_id, access_context=access_context)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "DOCUMENT_NOT_FOUND", "message": "문서를 찾을 수 없습니다."},
        )
    return document


@router.post(
    "/documents/{document_id}/source-sync-plans",
    response_model=ChangePlanView,
    status_code=status.HTTP_201_CREATED,
)
def create_source_sync_plan(
    document_id: str,
    request: SourceSyncRequest,
    repository: Annotated[KnowledgeRepository, Depends(get_knowledge_repository)],
    access_context: Annotated[AccessContext, Depends(get_access_context)],
    container: Annotated[AppContainer, Depends(get_container)],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=128),
    ],
) -> ChangePlanView:
    try:
        return container.plans.create_source_sync_plan(
            repository=repository,
            access_context=access_context,
            document_id=document_id,
            operation=request.operation,
            base_sha256=request.base_sha256,
            content=request.content,
            idempotency_key=idempotency_key,
        )
    except ChangeServiceError as error:
        raise change_http_error(error) from error
