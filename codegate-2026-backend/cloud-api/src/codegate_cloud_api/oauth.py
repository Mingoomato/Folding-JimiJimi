from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol

import httpx
import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWTError

from codegate_cloud_api.config import Settings
from codegate_cloud_api.models import GoogleIdentity


class OAuthProviderError(RuntimeError):
    pass


class GoogleIdentityProvider(Protocol):
    async def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        nonce: str,
    ) -> GoogleIdentity: ...

    async def close(self) -> None: ...


class GoogleOAuthClient:
    _ALGORITHMS = ["RS256", "ES256"]

    def __init__(self, settings: Settings) -> None:
        if not settings.google_client_id:
            raise ValueError("CODEGATE_GOOGLE_CLIENT_ID is required")
        self._client_id = settings.google_client_id
        self._client_secret = (
            settings.google_client_secret.get_secret_value()
            if settings.google_client_secret is not None
            else None
        )
        self._token_endpoint = settings.google_token_endpoint
        self._http = httpx.AsyncClient(timeout=settings.google_http_timeout_seconds)
        self._jwks = PyJWKClient(
            settings.google_jwks_url,
            cache_keys=True,
            lifespan=600,
            timeout=settings.google_http_timeout_seconds,
        )

    async def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        nonce: str,
    ) -> GoogleIdentity:
        payload = {
            "client_id": self._client_id,
            "code": code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        if self._client_secret:
            payload["client_secret"] = self._client_secret
        try:
            response = await self._http.post(self._token_endpoint, data=payload)
            response.raise_for_status()
            token_payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise OAuthProviderError("Google authorization code exchange failed") from error
        id_token = token_payload.get("id_token")
        if not isinstance(id_token, str):
            raise OAuthProviderError("Google token response did not include an ID token")
        return await asyncio.to_thread(self._verify_id_token, id_token, nonce)

    async def close(self) -> None:
        await self._http.aclose()

    def _verify_id_token(self, id_token: str, nonce: str) -> GoogleIdentity:
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(id_token)
            claims = jwt.decode(
                id_token,
                signing_key.key,
                algorithms=self._ALGORITHMS,
                audience=self._client_id,
                issuer=["accounts.google.com", "https://accounts.google.com"],
                options={"require": ["iss", "aud", "exp", "iat", "sub", "email", "nonce"]},
            )
        except PyJWTError as error:
            raise OAuthProviderError("Google ID token verification failed") from error
        if claims.get("nonce") != nonce:
            raise OAuthProviderError("Google ID token nonce did not match")
        if claims.get("email_verified") is not True:
            raise OAuthProviderError("Google account email is not verified")
        return GoogleIdentity(
            subject_id=_required_string(claims, "sub"),
            email=_required_string(claims, "email"),
            name=_optional_string(claims, "name"),
            picture=_optional_string(claims, "picture"),
        )


def _required_string(claims: Mapping[str, Any], key: str) -> str:
    value = claims.get(key)
    if not isinstance(value, str) or not value:
        raise OAuthProviderError(f"Google ID token {key} claim is invalid")
    return value


def _optional_string(claims: Mapping[str, Any], key: str) -> str | None:
    value = claims.get(key)
    return value if isinstance(value, str) and value else None
