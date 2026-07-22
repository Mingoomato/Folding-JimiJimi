from __future__ import annotations

import hashlib
import json
from typing import Any

from codegate_api.models import ChangePlanView

PLAN_HASH_DOMAIN = b"codegate.change-plan.v1\x00"
REQUEST_HASH_DOMAIN = b"codegate.idempotency-request.v1\x00"


def change_plan_hash(plan: ChangePlanView) -> str:
    payload = {
        "schema_version": "1.0.0",
        "change_plan_id": plan.change_plan_id,
        "document_id": plan.document_id,
        "file_version_id": plan.file_version_id,
        "source_uri": plan.source_uri,
        "base_sha256": plan.base_sha256,
        "proposed_sha256": plan.proposed_sha256,
        "operation": plan.operation.model_dump(mode="json"),
        "unified_diff": plan.unified_diff,
        "created_at": plan.created_at.isoformat(),
        "expires_at": plan.expires_at.isoformat(),
    }
    return _domain_hash(PLAN_HASH_DOMAIN, payload)


def request_hash(payload: dict[str, Any]) -> str:
    return _domain_hash(REQUEST_HASH_DOMAIN, payload)


def _domain_hash(domain: bytes, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(domain + canonical).hexdigest()
