from fastapi import HTTPException, status

from codegate_api.changes.service import ChangeServiceError


def change_http_error(error: ChangeServiceError) -> HTTPException:
    if error.code == "AUTHENTICATION_REQUIRED":
        code = status.HTTP_401_UNAUTHORIZED
    elif error.code.endswith("_NOT_FOUND") or error.code == "DOCUMENT_NOT_FOUND":
        code = status.HTTP_404_NOT_FOUND
    elif error.conflict:
        code = status.HTTP_409_CONFLICT
    else:
        code = status.HTTP_422_UNPROCESSABLE_CONTENT
    return HTTPException(
        status_code=code,
        detail={"code": error.code, "message": str(error)},
    )
