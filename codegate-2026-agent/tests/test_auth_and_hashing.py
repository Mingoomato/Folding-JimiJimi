from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jwt.exceptions import PyJWKClientError

from codegate_api.auth import (
    DemoTokenAuthenticator,
    SupabaseJwtAuthenticator,
    authenticate_authorization_header,
)
from codegate_api.changes.hashing import change_plan_hash, request_hash
from codegate_api.config import Settings
from codegate_api.knowledge.schemas import Editability, WriteAccess
from codegate_api.models import (
    ChangePlanStatus,
    ChangePlanView,
    ReplaceExactOperation,
)


def test_supabase_jwt_verifies_signature_issuer_audience_and_acl() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    settings = Settings(
        auth_mode="supabase",
        supabase_url="https://project.supabase.co",
    )
    authenticator = SupabaseJwtAuthenticator(settings)
    authenticator._jwks = SimpleNamespace(  # type: ignore[assignment]  # noqa: SLF001
        get_signing_key_from_jwt=lambda _: SimpleNamespace(key=public_key)
    )
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "iss": "https://project.supabase.co/auth/v1",
            "aud": "authenticated",
            "sub": "user-123",
            "exp": now + timedelta(minutes=5),
            "iat": now,
            "role": "authenticated",
            "aal": "aal1",
            "session_id": "session-123",
            "email": "user@example.com",
            "phone": "",
            "is_anonymous": False,
            "codegate_tenant_id": "tenant-a",
            "codegate_read_access": ["public", "internal", "invalid"],
            "codegate_write_document_ids": ["REG-000001"],
            "codegate_provisioned": True,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )

    context = authenticator.authenticate(token)

    assert context.subject_id == "user-123"
    assert context.tenant_id == "tenant-a"
    assert {level.value for level in context.readable_access} == {"public", "internal"}
    assert context.writable_document_ids == frozenset({"REG-000001"})
    assert context.provisioned is True
    assert context.email == "user@example.com"
    assert context.authz_source == "supabase-hook"


def test_unprovisioned_supabase_user_cannot_write_signed_document_claims() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    authenticator = SupabaseJwtAuthenticator(
        Settings(auth_mode="supabase", supabase_url="https://project.supabase.co")
    )
    authenticator._jwks = SimpleNamespace(  # type: ignore[assignment]  # noqa: SLF001
        get_signing_key_from_jwt=lambda _: SimpleNamespace(key=private_key.public_key())
    )
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "iss": "https://project.supabase.co/auth/v1",
            "aud": "authenticated",
            "sub": "user-123",
            "exp": now + timedelta(minutes=5),
            "iat": now,
            "role": "authenticated",
            "aal": "aal1",
            "session_id": "session-123",
            "email": "user@example.com",
            "phone": "",
            "is_anonymous": False,
            "codegate_tenant_id": "default",
            "codegate_read_access": ["public"],
            "codegate_write_document_ids": ["REG-000001"],
            "codegate_provisioned": False,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )

    context = authenticator.authenticate(token)

    assert context.provisioned is False
    assert context.writable_document_ids == frozenset({"REG-000001"})
    assert (
        context.can_write(
            SimpleNamespace(
                id="REG-000001",
                write_access=WriteAccess.RESTRICTED,
                editability=Editability.EDITABLE,
            )
        )
        is False
    )


@pytest.mark.parametrize(
    ("audience", "expires"),
    [
        ("wrong", timedelta(minutes=5)),
        ("authenticated", timedelta(seconds=-1)),
    ],
)
def test_supabase_jwt_rejects_wrong_audience_and_expiry(
    audience: str,
    expires: timedelta,
) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    authenticator = SupabaseJwtAuthenticator(
        Settings(auth_mode="supabase", supabase_url="https://project.supabase.co")
    )
    authenticator._jwks = SimpleNamespace(  # type: ignore[assignment]  # noqa: SLF001
        get_signing_key_from_jwt=lambda _: SimpleNamespace(key=private_key.public_key())
    )
    token = jwt.encode(
        {
            "iss": "https://project.supabase.co/auth/v1",
            "aud": audience,
            "sub": "user-123",
            "exp": datetime.now(UTC) + expires,
            "iat": datetime.now(UTC),
            "role": "authenticated",
            "aal": "aal1",
            "session_id": "session-123",
            "email": "user@example.com",
            "phone": "",
            "is_anonymous": False,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )

    with pytest.raises(HTTPException) as raised:
        authenticator.authenticate(token)

    assert raised.value.status_code == 401


def test_supabase_jwt_requires_hook_owned_tenant_claim() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    authenticator = SupabaseJwtAuthenticator(
        Settings(auth_mode="supabase", supabase_url="https://project.supabase.co")
    )
    authenticator._jwks = SimpleNamespace(  # type: ignore[assignment]  # noqa: SLF001
        get_signing_key_from_jwt=lambda _: SimpleNamespace(key=private_key.public_key())
    )
    token = jwt.encode(
        {
            "iss": "https://project.supabase.co/auth/v1",
            "aud": "authenticated",
            "sub": "user-123",
            "exp": datetime.now(UTC) + timedelta(minutes=5),
            "iat": datetime.now(UTC),
            "role": "authenticated",
            "aal": "aal1",
            "session_id": "session-123",
            "email": "user@example.com",
            "phone": "",
            "is_anonymous": False,
            "codegate_write_document_ids": ["REG-000001"],
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )

    with pytest.raises(HTTPException) as raised:
        authenticator.authenticate(token)

    assert raised.value.status_code == 401


def test_unknown_supabase_kids_are_negative_cached_and_refresh_throttled() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    authenticator = SupabaseJwtAuthenticator(
        Settings(auth_mode="supabase", supabase_url="https://project.supabase.co")
    )
    calls = 0

    def reject_unknown(_: str) -> None:
        nonlocal calls
        calls += 1
        raise PyJWKClientError("unknown signing key")

    authenticator._jwks = SimpleNamespace(  # type: ignore[assignment]  # noqa: SLF001
        get_signing_key_from_jwt=reject_unknown
    )
    now = datetime.now(UTC)
    claims = {
        "iss": "https://project.supabase.co/auth/v1",
        "aud": "authenticated",
        "sub": "user-123",
        "exp": now + timedelta(minutes=5),
        "iat": now,
        "role": "authenticated",
        "aal": "aal1",
        "session_id": "session-123",
        "email": "user@example.com",
        "phone": "",
        "is_anonymous": False,
    }
    tokens = [
        jwt.encode(
            claims,
            private_key,
            algorithm="RS256",
            headers={"kid": kid},
        )
        for kid in ("unknown-a", "unknown-b")
    ]

    for token in tokens:
        with pytest.raises(HTTPException) as raised:
            authenticator.authenticate(token)
        assert raised.value.status_code == 401

    assert calls == 1


def test_malformed_authorization_header_is_rejected() -> None:
    authenticator = DemoTokenAuthenticator(
        Settings(auth_mode="demo", demo_auth_token="demo-token-for-tests-only")  # gitleaks:allow
    )

    with pytest.raises(HTTPException) as raised:
        authenticate_authorization_header("Basic value", authenticator)

    assert raised.value.status_code == 401


def test_demo_auth_is_forbidden_in_production() -> None:
    with pytest.raises(ValueError, match="production requires CODEGATE_AUTH_MODE=supabase"):
        Settings(
            environment="production",
            auth_mode="demo",
            demo_auth_token="demo-token-for-tests-only",  # gitleaks:allow
        )


def test_plan_hash_covers_exact_diff_operation_and_result_hash() -> None:
    now = datetime.now(UTC)
    base = ChangePlanView(
        change_plan_id="plan_test",
        document_id="REG-000001",
        file_version_id="fv_1",
        source_uri="source://regulations/REG-000001.md",
        operation=ReplaceExactOperation(expected_text="1년", replacement_text="3년"),
        unified_diff="-1년\n+3년\n",
        base_sha256="1" * 64,
        proposed_sha256="2" * 64,
        plan_hash="0" * 64,
        status=ChangePlanStatus.PENDING_APPROVAL,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
    )
    original = change_plan_hash(base)

    assert change_plan_hash(base.model_copy(update={"unified_diff": "-1년\n+5년\n"})) != original
    assert change_plan_hash(base.model_copy(update={"proposed_sha256": "3" * 64})) != original
    assert (
        change_plan_hash(
            base.model_copy(
                update={
                    "operation": ReplaceExactOperation(
                        expected_text="1년",
                        replacement_text="5년",
                    )
                }
            )
        )
        != original
    )


def test_request_hash_uses_canonical_key_order() -> None:
    assert request_hash({"a": 1, "b": 2}) == request_hash({"b": 2, "a": 1})
