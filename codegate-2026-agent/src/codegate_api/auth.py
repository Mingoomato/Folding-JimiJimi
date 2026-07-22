from __future__ import annotations

import re
import secrets
import threading
import time
from collections.abc import Mapping
from typing import Any, Protocol

import jwt
from fastapi import HTTPException, status
from jwt import PyJWKClient
from jwt.exceptions import PyJWTError

from codegate_api.config import Settings
from codegate_api.knowledge.access import AccessContext
from codegate_api.knowledge.schemas import AccessLevel


class TokenAuthenticator(Protocol):
    def authenticate(self, token: str) -> AccessContext: ...

    def unauthenticated_context(self) -> AccessContext: ...


class DisabledAuthenticator:
    def authenticate(self, token: str) -> AccessContext:
        del token
        raise _unauthorized("인증이 설정되지 않았습니다.")

    def unauthenticated_context(self) -> AccessContext:
        return AccessContext.anonymous()


class LocalAuthenticator:
    """Single-user capability used only by the loopback-bound local application."""

    def __init__(self, settings: Settings) -> None:
        if settings.environment != "local":
            raise ValueError("local authentication requires CODEGATE_ENVIRONMENT=local")
        self._context = AccessContext(
            subject_id="local-user",
            tenant_id=settings.llmwiki_tenant_id,
            readable_access=frozenset(AccessLevel),
            writable_document_ids=frozenset(),
            provisioned=True,
            allow_all_writes=True,
            authz_source="local-workspace",
        )

    def authenticate(self, token: str) -> AccessContext:
        del token
        raise _unauthorized("로컬 모드는 Bearer token을 받지 않습니다.")

    def unauthenticated_context(self) -> AccessContext:
        return self._context


class DemoTokenAuthenticator:
    """Development-only bearer token adapter; never enabled implicitly."""

    def __init__(self, settings: Settings) -> None:
        if settings.environment == "production":
            raise ValueError("demo authentication is forbidden in production")
        if not settings.demo_auth_token or len(settings.demo_auth_token) < 16:
            raise ValueError("CODEGATE_DEMO_AUTH_TOKEN must contain at least 16 characters")
        self._token = settings.demo_auth_token
        self._context = AccessContext(
            subject_id=settings.demo_subject_id,
            tenant_id=settings.demo_tenant_id,
            readable_access=_parse_access_levels(settings.demo_read_access),
            writable_document_ids=frozenset(settings.demo_write_document_ids),
            provisioned=True,
            authz_source="demo-token",
        )

    def authenticate(self, token: str) -> AccessContext:
        if not secrets.compare_digest(token, self._token):
            raise _unauthorized("Bearer token이 유효하지 않습니다.")
        return self._context

    def unauthenticated_context(self) -> AccessContext:
        return AccessContext.anonymous()


class SupabaseJwtAuthenticator:
    """Verify asymmetric Supabase user JWTs against the project's JWKS."""

    _ALGORITHMS = ["ES256", "RS256", "EdDSA"]
    _KEY_TTL_SECONDS = 600.0
    _MIN_REFRESH_SECONDS = 5.0
    _UNKNOWN_KID_TTL_SECONDS = 60.0
    _MAX_UNKNOWN_KIDS = 1_024

    def __init__(self, settings: Settings) -> None:
        issuer = settings.effective_supabase_issuer()
        jwks_url = settings.effective_supabase_jwks_url()
        if not issuer or not jwks_url:
            raise ValueError("Supabase issuer and JWKS URL are required")
        self._issuer = issuer
        self._jwks = PyJWKClient(
            jwks_url,
            cache_keys=True,
            lifespan=int(self._KEY_TTL_SECONDS),
            timeout=settings.supabase_jwks_timeout_seconds,
        )
        self._jwks_lock = threading.Lock()
        self._signing_keys: dict[str, tuple[Any, float]] = {}
        self._unknown_kids: dict[str, float] = {}
        self._last_jwks_refresh_at = float("-inf")
        self._audience = settings.supabase_jwt_audience
        self._tenant_claim = settings.supabase_tenant_claim
        self._read_access_claim = settings.supabase_read_access_claim
        self._write_documents_claim = settings.supabase_write_documents_claim

    def unauthenticated_context(self) -> AccessContext:
        return AccessContext.anonymous()

    def authenticate(self, token: str) -> AccessContext:
        try:
            signing_key = self._signing_key(token)
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=self._ALGORITHMS,
                audience=self._audience,
                issuer=self._issuer,
                options={
                    "require": [
                        "iss",
                        "aud",
                        "exp",
                        "iat",
                        "sub",
                        "role",
                        "aal",
                        "session_id",
                        "email",
                        "phone",
                        "is_anonymous",
                    ]
                },
            )
        except PyJWTError as error:
            raise _unauthorized("Supabase access token이 유효하지 않습니다.") from error

        subject_id = _required_string(claims, "sub")
        if claims.get("role") != "authenticated":
            raise _unauthorized("Supabase JWT role claim이 유효하지 않습니다.")
        tenant_id = _required_string(claims, self._tenant_claim)
        access_values = _string_list(claims.get(self._read_access_claim, ["public"]))
        write_document_ids = [
            item
            for item in _string_list(claims.get(self._write_documents_claim, []))[:100]
            if re.fullmatch(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+", item) and len(item) <= 96
        ]
        provisioned = claims.get("codegate_provisioned") is True
        return AccessContext(
            subject_id=subject_id,
            tenant_id=tenant_id,
            readable_access=_parse_access_levels(access_values),
            writable_document_ids=frozenset(write_document_ids),
            provisioned=provisioned,
            email=_optional_string(claims, "email"),
            authz_source="supabase-hook",
        )

    def _signing_key(self, token: str) -> Any:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        algorithm = header.get("alg")
        if (
            not isinstance(kid, str)
            or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", kid)
            or algorithm not in self._ALGORITHMS
        ):
            raise _unauthorized("Supabase JWT header가 유효하지 않습니다.")

        now = time.monotonic()
        with self._jwks_lock:
            cached = self._signing_keys.get(kid)
            if cached is not None and cached[1] > now:
                return cached[0]
            negative_until = self._unknown_kids.get(kid, 0.0)
            if negative_until > now:
                raise _unauthorized("Supabase access token이 유효하지 않습니다.")
            if now - self._last_jwks_refresh_at < self._MIN_REFRESH_SECONDS:
                self._remember_unknown_kid(kid, now)
                raise _unauthorized("Supabase access token이 유효하지 않습니다.")

            self._last_jwks_refresh_at = now
            try:
                signing_key = self._jwks.get_signing_key_from_jwt(token)
            except PyJWTError:
                self._remember_unknown_kid(kid, now)
                raise
            resolved_kid = getattr(signing_key, "key_id", kid)
            if resolved_kid != kid:
                self._remember_unknown_kid(kid, now)
                raise _unauthorized("Supabase JWT signing key가 일치하지 않습니다.")
            self._signing_keys[kid] = (signing_key.key, now + self._KEY_TTL_SECONDS)
            self._unknown_kids.pop(kid, None)
            while len(self._signing_keys) > 32:
                self._signing_keys.pop(next(iter(self._signing_keys)))
            return signing_key.key

    def _remember_unknown_kid(self, kid: str, now: float) -> None:
        expired = [key for key, deadline in self._unknown_kids.items() if deadline <= now]
        for key in expired:
            self._unknown_kids.pop(key, None)
        self._unknown_kids[kid] = now + self._UNKNOWN_KID_TTL_SECONDS
        while len(self._unknown_kids) > self._MAX_UNKNOWN_KIDS:
            self._unknown_kids.pop(next(iter(self._unknown_kids)))


def build_authenticator(settings: Settings) -> TokenAuthenticator:
    if settings.auth_mode == "demo":
        return DemoTokenAuthenticator(settings)
    if settings.auth_mode == "supabase":
        return SupabaseJwtAuthenticator(settings)
    if settings.auth_mode == "local":
        return LocalAuthenticator(settings)
    return DisabledAuthenticator()


def authenticate_authorization_header(
    authorization: str | None,
    authenticator: TokenAuthenticator,
) -> AccessContext:
    if authorization is None:
        return authenticator.unauthenticated_context()
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized("Authorization 헤더는 Bearer 형식이어야 합니다.")
    return authenticator.authenticate(token.strip())


def _parse_access_levels(values: list[str]) -> frozenset[AccessLevel]:
    parsed: set[AccessLevel] = {AccessLevel.PUBLIC}
    for value in values:
        try:
            parsed.add(AccessLevel(value))
        except ValueError:
            continue
    return frozenset(parsed)


def _required_string(claims: Mapping[str, Any], key: str) -> str:
    value = claims.get(key)
    if not isinstance(value, str) or not value:
        raise _unauthorized(f"JWT {key} claim이 유효하지 않습니다.")
    return value


def _optional_string(claims: Mapping[str, Any], key: str) -> str | None:
    value = claims.get(key)
    return value if isinstance(value, str) and value else None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return []
    return value


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "UNAUTHORIZED", "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )
