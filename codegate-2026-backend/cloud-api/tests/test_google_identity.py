from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from codegate_cloud_api.oauth import GoogleOAuthClient, OAuthProviderError


def _client(public_key) -> GoogleOAuthClient:  # type: ignore[no-untyped-def]
    client = object.__new__(GoogleOAuthClient)
    client._client_id = "desktop-client.apps.googleusercontent.com"  # noqa: SLF001
    client._jwks = SimpleNamespace(  # noqa: SLF001
        get_signing_key_from_jwt=lambda _: SimpleNamespace(key=public_key)
    )
    return client


def _token(private_key, **overrides) -> str:  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    claims = {
        "iss": "https://accounts.google.com",
        "aud": "desktop-client.apps.googleusercontent.com",
        "exp": now + timedelta(minutes=5),
        "iat": now,
        "sub": "google-user-123",
        "email": "user@example.com",
        "email_verified": True,
        "nonce": "expected-nonce",
        "name": "테스트 사용자",
    }
    claims.update(overrides)
    return jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )


def test_google_id_token_verifies_identity_and_nonce() -> None:
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    client = _client(private_key.public_key())

    identity = client._verify_id_token(  # noqa: SLF001
        _token(private_key),
        "expected-nonce",
    )

    assert identity.subject_id == "google-user-123"
    assert identity.email == "user@example.com"
    assert identity.name == "테스트 사용자"


@pytest.mark.parametrize(
    "overrides,nonce",
    [
        ({"aud": "wrong-client"}, "expected-nonce"),
        ({"email_verified": False}, "expected-nonce"),
        ({}, "wrong-nonce"),
    ],
)
def test_google_id_token_rejects_invalid_security_claims(overrides, nonce) -> None:  # type: ignore[no-untyped-def]
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    client = _client(private_key.public_key())

    with pytest.raises(OAuthProviderError):
        client._verify_id_token(_token(private_key, **overrides), nonce)  # noqa: SLF001
