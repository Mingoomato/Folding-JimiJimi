from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestBodyLimitMiddleware:
    """Bound both Content-Length and chunked HTTP request bodies before parsing."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            await self._app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                await self._reject(scope, receive, send, headers)
                return
            if declared_size < 0 or declared_size > self._max_bytes:
                await self._reject(scope, receive, send, headers)
                return

        messages: list[Message] = []
        received_bytes = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            messages.append(message)
            if message["type"] != "http.request":
                continue
            received_bytes += len(message.get("body", b""))
            if received_bytes > self._max_bytes:
                await self._reject(scope, receive, send, headers)
                return
            if not message.get("more_body", False):
                break

        index = 0

        async def replay() -> Message:
            nonlocal index
            if index >= len(messages):
                return {"type": "http.disconnect"}
            message = messages[index]
            index += 1
            return message

        await self._app(scope, replay, send)

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        headers: dict[bytes, bytes],
    ) -> None:
        request_id = headers.get(b"x-request-id", b"").decode("ascii", errors="ignore")
        response_headers = {"X-Request-ID": request_id} if 0 < len(request_id) <= 128 else None
        response = JSONResponse(
            status_code=413,
            content={
                "detail": {
                    "code": "REQUEST_TOO_LARGE",
                    "message": "요청 본문 크기 제한을 초과했습니다.",
                }
            },
            headers=response_headers,
        )
        await response(scope, receive, send)
