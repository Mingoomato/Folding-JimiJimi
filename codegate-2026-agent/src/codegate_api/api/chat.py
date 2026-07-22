from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status

from codegate_api.container import AppContainer
from codegate_api.dependencies import get_access_context, get_container, get_knowledge_repository
from codegate_api.knowledge.access import AccessContext
from codegate_api.knowledge.repository import KnowledgeRepository
from codegate_api.models import ChatMessageRequest, ChatMessageResponse
from codegate_api.state.store import StateConflict

router = APIRouter(tags=["chat"])


@router.post("/chat/messages", response_model=ChatMessageResponse)
async def create_message(
    request: ChatMessageRequest,
    repository: Annotated[KnowledgeRepository, Depends(get_knowledge_repository)],
    access_context: Annotated[AccessContext, Depends(get_access_context)],
    container: Annotated[AppContainer, Depends(get_container)],
    idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
) -> ChatMessageResponse:
    try:
        return await container.chat.create_message(
            request=request,
            repository=repository,
            access_context=access_context,
            idempotency_key=idempotency_key,
        )
    except StateConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": error.code, "message": str(error)},
        ) from error
