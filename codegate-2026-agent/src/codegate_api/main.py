from __future__ import annotations

import hashlib
import ipaddress
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from codegate_api.api import auth_session, change_plans, chat, demo, documents, executions, health
from codegate_api.config import Settings, get_settings
from codegate_api.container import build_container
from codegate_api.docs_page import render_api_docs
from codegate_api.rate_limit import FixedWindowRateLimiter
from codegate_api.request_limits import RequestBodyLimitMiddleware


def _normalized_caller_identity(authorization: str | None, client_host: str) -> str:
    if authorization:
        scheme, separator, token = authorization.partition(" ")
        normalized_token = token.strip()
        if separator and scheme.lower() == "bearer" and normalized_token:
            digest = hashlib.sha256(normalized_token.encode()).hexdigest()
            return f"bearer:{digest}"
    return f"anonymous:{client_host}"


def _is_loopback_client(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def create_app(settings: Settings | None = None) -> FastAPI:
    application_settings = settings or get_settings()
    container = build_container(application_settings)
    chat_rate_limiter = FixedWindowRateLimiter(
        limit=application_settings.chat_rate_limit_requests,
        window_seconds=application_settings.chat_rate_limit_window_seconds,
    )
    chat_ip_rate_limiter = FixedWindowRateLimiter(
        limit=application_settings.chat_rate_limit_requests * 4,
        window_seconds=application_settings.chat_rate_limit_window_seconds,
    )
    auth_ip_rate_limiter = FixedWindowRateLimiter(
        limit=application_settings.auth_rate_limit_requests,
        window_seconds=application_settings.auth_rate_limit_window_seconds,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await container.startup()
        try:
            yield
        finally:
            await container.shutdown()

    application = FastAPI(
        title=application_settings.app_name,
        version="0.2.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    application.state.container = container
    application.add_middleware(
        CORSMiddleware,
        allow_origins=application_settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
    )

    @application.middleware("http")
    async def request_guards(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get("X-Request-ID")
        if not request_id or len(request_id) > 128:
            request_id = str(uuid4())
        authorization = request.headers.get("Authorization")
        client_host = request.client.host if request.client else "unknown"
        if application_settings.environment == "local" and not _is_loopback_client(client_host):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": {
                        "code": "LOCAL_ACCESS_ONLY",
                        "message": "로컬 앱 API는 loopback 요청만 허용합니다.",
                    }
                },
                headers={"X-Request-ID": request_id},
            )
        if authorization and len(authorization.encode("utf-8")) > (
            application_settings.authorization_header_max_bytes
        ):
            return JSONResponse(
                status_code=431,
                content={
                    "detail": {
                        "code": "REQUEST_HEADER_TOO_LARGE",
                        "message": "Authorization 헤더 크기 제한을 초과했습니다.",
                    }
                },
                headers={"X-Request-ID": request_id},
            )
        if authorization and not await auth_ip_rate_limiter.allow(f"auth-ip:{client_host}"):
            return JSONResponse(
                status_code=429,
                content={
                    "detail": {
                        "code": "RATE_LIMITED",
                        "message": "인증 요청 한도를 초과했습니다.",
                    }
                },
                headers={
                    "X-Request-ID": request_id,
                    "Retry-After": str(auth_ip_rate_limiter.retry_after_seconds),
                },
            )
        if request.method == "POST" and request.url.path == "/api/v1/chat/messages":
            caller_identity = _normalized_caller_identity(authorization, client_host)
            allowed_by_ip = await chat_ip_rate_limiter.allow(f"ip:{client_host}")
            allowed_by_caller = await chat_rate_limiter.allow(caller_identity)
            if not allowed_by_ip or not allowed_by_caller:
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": {
                            "code": "RATE_LIMITED",
                            "message": "채팅 요청 한도를 초과했습니다.",
                        }
                    },
                    headers={
                        "X-Request-ID": request_id,
                        "Retry-After": str(chat_rate_limiter.retry_after_seconds),
                    },
                )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    application.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=application_settings.max_request_bytes,
    )

    @application.get("/", include_in_schema=False)
    async def api_root() -> RedirectResponse:
        return RedirectResponse(url="/docs", status_code=307)

    @application.get("/docs", include_in_schema=False)
    async def api_docs() -> Response:
        return render_api_docs(
            app_name=application.title,
            version=application.version,
            schema=application.openapi(),
        )

    application.include_router(health.router, prefix="/api/v1")
    application.include_router(auth_session.router, prefix="/api/v1")
    application.include_router(chat.router, prefix="/api/v1")
    application.include_router(documents.router, prefix="/api/v1")
    application.include_router(change_plans.router, prefix="/api/v1")
    application.include_router(executions.router, prefix="/api/v1")
    application.include_router(demo.router, prefix="/api/v1")
    return application


app = create_app()
