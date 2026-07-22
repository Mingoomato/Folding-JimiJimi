from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response, status

from codegate_api.api.errors import change_http_error
from codegate_api.changes.service import ChangeServiceError
from codegate_api.container import AppContainer
from codegate_api.dependencies import get_access_context, get_container
from codegate_api.knowledge.access import AccessContext
from codegate_api.models import ExecutionView

router = APIRouter(tags=["executions"])


@router.get("/executions/{execution_id}", response_model=ExecutionView)
def get_execution(
    execution_id: str,
    access_context: Annotated[AccessContext, Depends(get_access_context)],
    container: Annotated[AppContainer, Depends(get_container)],
) -> ExecutionView:
    try:
        return container.executions.get_execution(
            execution_id,
            access_context=access_context,
        )
    except ChangeServiceError as error:
        raise change_http_error(error) from error


@router.post(
    "/executions/{execution_id}/retry-sync",
    response_model=ExecutionView,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_execution_sync(
    execution_id: str,
    access_context: Annotated[AccessContext, Depends(get_access_context)],
    container: Annotated[AppContainer, Depends(get_container)],
    response: Response,
) -> ExecutionView:
    try:
        execution = container.executions.retry_sync(
            execution_id,
            access_context=access_context,
        )
        response.headers["Location"] = f"/api/v1/executions/{execution.execution_id}"
        return execution
    except ChangeServiceError as error:
        raise change_http_error(error) from error


@router.post(
    "/executions/{execution_id}/undo",
    response_model=ExecutionView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def undo_execution(
    execution_id: str,
    access_context: Annotated[AccessContext, Depends(get_access_context)],
    container: Annotated[AppContainer, Depends(get_container)],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=128),
    ],
    response: Response,
) -> ExecutionView:
    try:
        execution = await container.executions.undo(
            execution_id=execution_id,
            access_context=access_context,
            idempotency_key=idempotency_key,
        )
        response.headers["Location"] = f"/api/v1/executions/{execution.execution_id}"
        return execution
    except ChangeServiceError as error:
        raise change_http_error(error) from error
