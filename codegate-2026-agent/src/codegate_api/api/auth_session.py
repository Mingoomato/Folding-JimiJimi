from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status

from codegate_api.dependencies import get_access_context
from codegate_api.knowledge.access import AccessContext
from codegate_api.models import AuthMeResponse

router = APIRouter(tags=["auth"])


@router.get("/auth/me", response_model=AuthMeResponse)
async def get_current_user(
    response: Response,
    access_context: Annotated[AccessContext, Depends(get_access_context)],
) -> AuthMeResponse:
    response.headers["Cache-Control"] = "private, no-store"
    if access_context.subject_id is None or access_context.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "로그인이 필요합니다."},
            headers={
                "WWW-Authenticate": "Bearer",
                "Cache-Control": "private, no-store",
            },
        )
    write_scope: Literal["none", "documents", "workspace"] = (
        "workspace" if access_context.allow_all_writes else "documents"
    )
    if not access_context.provisioned or (
        not access_context.allow_all_writes and not access_context.writable_document_ids
    ):
        write_scope = "none"
    return AuthMeResponse(
        subject_id=access_context.subject_id,
        tenant_id=access_context.tenant_id,
        email=access_context.email,
        read_access=sorted(level.value for level in access_context.readable_access),
        write_scope=write_scope,
        writable_document_ids=sorted(access_context.writable_document_ids),
        provisioned=access_context.provisioned,
        authz_source=access_context.authz_source,
    )
