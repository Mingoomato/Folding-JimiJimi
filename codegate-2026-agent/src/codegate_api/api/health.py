from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from codegate_api.container import AppContainer
from codegate_api.dependencies import get_container, get_knowledge_repository
from codegate_api.knowledge.repository import KnowledgeRepository
from codegate_api.models import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health(
    repository: Annotated[KnowledgeRepository, Depends(get_knowledge_repository)],
    container: Annotated[AppContainer, Depends(get_container)],
    response: Response,
) -> HealthResponse:
    converter_available = container.converter_available
    agent_available = container.agent_available
    persistence_available = container.state.healthcheck()
    worker_available = container.sync_worker.healthy
    pending_sync_events, failed_sync_events = container.state.sync_event_counts()
    stale_sync_events = container.state.stale_sync_event_count(
        stale_after_seconds=container.settings.sync_stale_after_seconds
    )
    graph_available = repository.graph_available
    index_artifacts_available = repository.index_artifacts_available
    ready = (
        converter_available
        and agent_available
        and persistence_available
        and graph_available
        and worker_available
        and failed_sync_events == 0
        and stale_sync_events == 0
        and (container.settings.environment != "production" or index_artifacts_available)
    )
    if container.settings.environment == "production" and not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if ready else "degraded",
        knowledge_version=repository.version,
        converter_available=converter_available,
        graph_available=graph_available,
        index_artifacts_available=index_artifacts_available,
        worker_available=worker_available,
        pending_sync_events=pending_sync_events,
        failed_sync_events=failed_sync_events,
        stale_sync_events=stale_sync_events,
        agent_available=agent_available,
        auth_mode=container.settings.auth_mode,
        persistence_available=persistence_available,
    )
