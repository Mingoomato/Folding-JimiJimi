from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from codegate_api.container import AppContainer
from codegate_api.dependencies import get_access_context, get_container
from codegate_api.knowledge.access import AccessContext
from codegate_api.models import DemoResetResponse

router = APIRouter(tags=["demo"])


@router.post("/demo/reset", response_model=DemoResetResponse)
async def reset_demo(
    access_context: Annotated[AccessContext, Depends(get_access_context)],
    container: Annotated[AppContainer, Depends(get_container)],
) -> DemoResetResponse:
    if (
        container.settings.auth_mode != "demo"
        or access_context.subject_id != container.settings.demo_subject_id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "endpoint를 찾을 수 없습니다."},
        )
    version = await container.reset_demo()
    return DemoResetResponse(status="reset", knowledge_version=version)
