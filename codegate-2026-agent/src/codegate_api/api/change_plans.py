from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response, status

from codegate_api.api.errors import change_http_error
from codegate_api.changes.service import ChangeExecutionService, ChangeServiceError
from codegate_api.container import AppContainer
from codegate_api.dependencies import get_access_context, get_container
from codegate_api.knowledge.access import AccessContext
from codegate_api.models import (
    ApproveChangePlanRequest,
    ExecutionView,
    RejectChangePlanRequest,
    RejectChangePlanResponse,
)

router = APIRouter(tags=["change-plans"])


def _service(container: AppContainer) -> ChangeExecutionService:
    return container.executions


@router.post(
    "/change-plans/{change_plan_id}/approve",
    response_model=ExecutionView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def approve_change_plan(
    change_plan_id: str,
    body: ApproveChangePlanRequest,
    access_context: Annotated[AccessContext, Depends(get_access_context)],
    container: Annotated[AppContainer, Depends(get_container)],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=128),
    ],
    response: Response,
) -> ExecutionView:
    try:
        execution = await _service(container).approve(
            plan_id=change_plan_id,
            supplied_plan_hash=body.plan_hash,
            access_context=access_context,
            idempotency_key=idempotency_key,
        )
        response.headers["Location"] = f"/api/v1/executions/{execution.execution_id}"
        return execution
    except ChangeServiceError as error:
        raise change_http_error(error) from error


@router.post("/change-plans/{change_plan_id}/reject", response_model=RejectChangePlanResponse)
def reject_change_plan(
    change_plan_id: str,
    body: RejectChangePlanRequest,
    access_context: Annotated[AccessContext, Depends(get_access_context)],
    container: Annotated[AppContainer, Depends(get_container)],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=128),
    ],
) -> RejectChangePlanResponse:
    try:
        return _service(container).reject(
            plan_id=change_plan_id,
            supplied_plan_hash=body.plan_hash,
            reason=body.reason,
            access_context=access_context,
            idempotency_key=idempotency_key,
        )
    except ChangeServiceError as error:
        raise change_http_error(error) from error
