from __future__ import annotations

from typing import Annotated

from fastapi import Header, Request

from codegate_api.auth import authenticate_authorization_header
from codegate_api.container import AppContainer
from codegate_api.knowledge.access import AccessContext
from codegate_api.knowledge.repository import KnowledgeRepository


def get_container(request: Request) -> AppContainer:
    container: AppContainer = request.app.state.container
    return container


def get_knowledge_repository(request: Request) -> KnowledgeRepository:
    return get_container(request).catalog.snapshot()


def get_access_context(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> AccessContext:
    return authenticate_authorization_header(
        authorization,
        get_container(request).authenticator,
    )
