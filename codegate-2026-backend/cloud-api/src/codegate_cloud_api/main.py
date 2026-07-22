import asyncio
import threading
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated
from urllib.parse import urlencode, urlsplit

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from codegate_cloud_api.config import Settings, get_settings
from codegate_cloud_api.models import (
    OAuthExchangeRequest,
    OAuthStartRequest,
    OAuthStartResponse,
    SessionRefreshRequest,
    SessionResponse,
    UserResponse,
)
from codegate_cloud_api.oauth import (
    GoogleIdentityProvider,
    GoogleOAuthClient,
    OAuthProviderError,
)
from codegate_cloud_api.store import (
    AuthStore,
    OAuthFlowCapacityError,
    StoredUser,
    verify_pkce,
)


def create_app(
    settings: Settings | None = None,
    *,
    store: AuthStore | None = None,
    provider: GoogleIdentityProvider | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    owns_store = store is None
    auth_store = store or AuthStore(
        resolved_settings.database_path,
        resolved_settings.resolved_session_pepper(),
        max_pending_oauth_flows=resolved_settings.max_pending_oauth_flows,
        max_refresh_rotations_per_family=resolved_settings.max_refresh_rotations_per_family,
    )
    oauth_start_limiter = _IpRateLimiter(
        limit=resolved_settings.oauth_start_rate_limit_requests,
        window_seconds=resolved_settings.oauth_start_rate_limit_window_seconds,
    )
    refresh_limiter = _IpRateLimiter(
        limit=resolved_settings.refresh_rate_limit_requests,
        window_seconds=resolved_settings.refresh_rate_limit_window_seconds,
    )
    owns_provider = provider is None
    identity_provider = provider
    if identity_provider is None and resolved_settings.google_client_id:
        identity_provider = GoogleOAuthClient(resolved_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        cleanup_stop = asyncio.Event()
        cleanup_task = asyncio.create_task(
            _periodic_cleanup(
                auth_store,
                resolved_settings.expired_data_cleanup_interval_seconds,
                cleanup_stop,
            )
        )
        try:
            yield
        finally:
            cleanup_stop.set()
            await cleanup_task
            if owns_provider and identity_provider is not None:
                await identity_provider.close()
            if owns_store:
                auth_store.close()

    app = FastAPI(title="CODEGATE Cloud API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=False,
    )

    @app.middleware("http")
    async def limit_authorization_header(request, call_next):  # type: ignore[no-untyped-def]
        authorization = request.headers.get("authorization")
        if (
            authorization
            and len(authorization.encode()) > resolved_settings.authorization_header_max_bytes
        ):
            return JSONResponse(
                status_code=status.HTTP_431_REQUEST_HEADER_FIELDS_TOO_LARGE,
                content={
                    "detail": {
                        "code": "HEADER_TOO_LARGE",
                        "message": "Authorization 헤더가 너무 큽니다.",
                    }
                },
            )
        return await call_next(request)

    def current_user(authorization: str | None = Header(default=None)) -> StoredUser:
        token = _bearer_token(authorization)
        user = auth_store.user_for_access_token(token)
        if user is None:
            raise _unauthorized("세션이 만료되었거나 유효하지 않습니다.")
        return user

    @app.get("/api/v1/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "oauth_provider": "google" if identity_provider is not None else "unconfigured",
        }

    @app.post("/api/v1/auth/google/start", response_model=OAuthStartResponse)
    async def start_google_oauth(
        request: OAuthStartRequest,
        response: Response,
        http_request: Request,
    ) -> OAuthStartResponse:
        if identity_provider is None or not resolved_settings.google_client_id:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "OAUTH_UNCONFIGURED",
                    "message": "Google OAuth가 설정되지 않았습니다.",
                },
            )
        client_ip = http_request.client.host if http_request.client is not None else "unknown"
        if not oauth_start_limiter.allow(client_ip):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "OAUTH_RATE_LIMITED",
                    "message": "잠시 후 Google 로그인을 다시 시도해 주세요.",
                },
                headers={
                    "Retry-After": str(resolved_settings.oauth_start_rate_limit_window_seconds)
                },
            )
        redirect_uri = _validated_loopback_redirect(request.redirect_uri)
        try:
            flow = auth_store.create_oauth_flow(
                redirect_uri=redirect_uri,
                code_challenge=request.code_challenge,
                ttl_seconds=resolved_settings.oauth_flow_ttl_seconds,
            )
        except OAuthFlowCapacityError as error:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "OAUTH_CAPACITY_REACHED",
                    "message": "진행 중인 로그인 요청이 많습니다. 잠시 후 다시 시도해 주세요.",
                },
                headers={"Retry-After": str(resolved_settings.oauth_flow_ttl_seconds)},
            ) from error
        _prevent_caching(response)
        query = urlencode(
            {
                "client_id": resolved_settings.google_client_id,
                "redirect_uri": flow.redirect_uri,
                "response_type": "code",
                "scope": "openid email profile",
                "state": flow.state,
                "nonce": flow.nonce,
                "code_challenge": flow.code_challenge,
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )
        return OAuthStartResponse(
            authorization_url=f"{resolved_settings.google_authorization_endpoint}?{query}",
            state=flow.state,
            expires_at=flow.expires_at,
        )

    @app.post("/api/v1/auth/google/exchange", response_model=SessionResponse)
    async def exchange_google_code(
        request: OAuthExchangeRequest,
        response: Response,
    ) -> SessionResponse:
        if identity_provider is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "OAUTH_UNCONFIGURED",
                    "message": "Google OAuth가 설정되지 않았습니다.",
                },
            )
        flow = auth_store.consume_oauth_flow(request.state)
        if flow is None:
            raise _unauthorized("OAuth 요청이 만료되었거나 이미 사용되었습니다.")
        if not verify_pkce(request.code_verifier, flow.code_challenge):
            raise _unauthorized("OAuth PKCE 검증에 실패했습니다.")
        try:
            identity = await identity_provider.exchange_code(
                code=request.code,
                code_verifier=request.code_verifier,
                redirect_uri=flow.redirect_uri,
                nonce=flow.nonce,
            )
        except OAuthProviderError as error:
            raise _unauthorized("Google 로그인을 완료하지 못했습니다.") from error
        access_token, refresh_token = auth_store.create_session(
            identity,
            access_ttl_seconds=resolved_settings.access_token_ttl_seconds,
            refresh_ttl_seconds=resolved_settings.refresh_token_ttl_seconds,
        )
        _prevent_caching(response)
        return SessionResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=resolved_settings.access_token_ttl_seconds,
        )

    @app.post("/api/v1/auth/refresh", response_model=SessionResponse)
    async def refresh_session(
        request: SessionRefreshRequest,
        response: Response,
        http_request: Request,
    ) -> SessionResponse:
        client_ip = http_request.client.host if http_request.client is not None else "unknown"
        if not refresh_limiter.allow(client_ip):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "REFRESH_RATE_LIMITED",
                    "message": "잠시 후 session을 다시 갱신해 주세요.",
                },
                headers={"Retry-After": str(resolved_settings.refresh_rate_limit_window_seconds)},
            )
        rotated = auth_store.rotate_session(
            request.refresh_token,
            access_ttl_seconds=resolved_settings.access_token_ttl_seconds,
        )
        if rotated is None:
            raise _unauthorized("Refresh token이 만료되었거나 유효하지 않습니다.")
        _prevent_caching(response)
        return SessionResponse(
            access_token=rotated[0],
            refresh_token=rotated[1],
            expires_in=resolved_settings.access_token_ttl_seconds,
        )

    @app.get("/api/v1/auth/me", response_model=UserResponse)
    async def auth_me(
        response: Response,
        user: Annotated[StoredUser, Depends(current_user)],
    ) -> UserResponse:
        _prevent_caching(response)
        return UserResponse.model_validate(user)

    @app.post("/api/v1/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(
        user_token: Annotated[str, Depends(_access_token_dependency)],
    ) -> Response:
        auth_store.revoke_access_token(user_token)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app


def _validated_loopback_redirect(value: str) -> str:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "INVALID_REDIRECT_URI",
                "message": "redirect_uri는 포트가 있는 loopback HTTP 주소여야 합니다.",
            },
        )
    return value


def _access_token_dependency(authorization: str | None = Header(default=None)) -> str:
    return _bearer_token(authorization)


def _bearer_token(authorization: str | None) -> str:
    if authorization is None:
        raise _unauthorized("로그인이 필요합니다.")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized("Authorization 헤더는 Bearer 형식이어야 합니다.")
    return token.strip()


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "UNAUTHORIZED", "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _prevent_caching(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"


class _IpRateLimiter:
    def __init__(self, *, limit: int, window_seconds: int, max_keys: int = 10_000) -> None:
        self._limit = limit
        self._window_seconds = float(window_seconds)
        self._max_keys = max_keys
        self._requests: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self._window_seconds
        with self._lock:
            timestamps = self._requests.get(key)
            if timestamps is None:
                if len(self._requests) >= self._max_keys:
                    self._prune(cutoff)
                if len(self._requests) >= self._max_keys:
                    return False
                timestamps = deque()
                self._requests[key] = timestamps
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if len(timestamps) >= self._limit:
                return False
            timestamps.append(now)
            return True

    def _prune(self, cutoff: float) -> None:
        expired_keys = [
            key
            for key, timestamps in self._requests.items()
            if not timestamps or timestamps[-1] <= cutoff
        ]
        for key in expired_keys:
            self._requests.pop(key, None)


async def _periodic_cleanup(
    store: AuthStore,
    interval_seconds: int,
    stop: asyncio.Event,
) -> None:
    while True:
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        if stop.is_set():
            return
        await asyncio.to_thread(store.cleanup_expired)


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.api_host,
        port=settings.api_port,
    )
