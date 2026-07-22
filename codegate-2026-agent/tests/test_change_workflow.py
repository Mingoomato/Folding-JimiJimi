from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codegate_api.auth import authenticate_authorization_header
from codegate_api.changes.service import ChangePlanService, ChangeServiceError
from codegate_api.config import Settings
from codegate_api.files.atomic import SafeSourceFileStore
from codegate_api.files.resolver import SourceUriResolver
from codegate_api.knowledge.access import AccessContext
from codegate_api.knowledge.schemas import AccessLevel, ManifestEntry
from codegate_api.models import ReplaceExactOperation
from codegate_api.state.store import StateStore


def _preview(
    client: TestClient,
    auth_headers: dict[str, str],
    *,
    old: str = "1년",
    new: str = "3년",
    conversation_id: str = "change-demo",
) -> dict[str, object]:
    response = client.post(
        "/api/v1/chat/messages",
        headers=auth_headers,
        json={
            "conversation_id": conversation_id,
            "message": f'REG-000001에서 "{old}"을 "{new}"으로 변경해줘',
            "selected_document_id": "REG-000001",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["response_type"] == "change_preview"
    return payload["change_plan"]


def _approve(
    client: TestClient,
    auth_headers: dict[str, str],
    plan: dict[str, object],
    *,
    key: str = "approve-key-0001",
) -> object:
    return client.post(
        f"/api/v1/change-plans/{plan['change_plan_id']}/approve",
        headers={**auth_headers, "Idempotency-Key": key},
        json={"plan_hash": plan["plan_hash"]},
    )


def _wait_for_execution(
    client: TestClient,
    auth_headers: dict[str, str],
    execution_id: str,
    *,
    allow_sync_failure: bool = False,
) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/executions/{execution_id}", headers=auth_headers)
        assert response.status_code == 200
        execution = response.json()
        if execution["terminal"] or (allow_sync_failure and execution["sync_status"] == "failed"):
            return execution
        time.sleep(0.01)
    raise AssertionError(f"execution did not reach the expected state: {execution}")


def test_preview_does_not_write_source(
    client: TestClient,
    auth_headers: dict[str, str],
    app_settings: Settings,
) -> None:
    source = app_settings.resolved_source_root() / "regulations/REG-000001.md"
    before = source.read_bytes()

    plan = _preview(client, auth_headers)

    assert source.read_bytes() == before
    assert "-개인정보는" in str(plan["unified_diff"])
    assert "+개인정보는" in str(plan["unified_diff"])


def test_anonymous_change_request_cannot_create_plan(client: TestClient) -> None:
    response = client.post(
        "/api/v1/chat/messages",
        json={
            "conversation_id": "anonymous-change",
            "message": 'REG-000001에서 "1년"을 "3년"으로 변경해줘',
            "selected_document_id": "REG-000001",
        },
    )

    assert response.status_code == 200
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_wrong_plan_hash_writes_nothing(
    client: TestClient,
    auth_headers: dict[str, str],
    app_settings: Settings,
) -> None:
    plan = _preview(client, auth_headers)
    source = app_settings.resolved_source_root() / "regulations/REG-000001.md"
    before = source.read_bytes()

    response = client.post(
        f"/api/v1/change-plans/{plan['change_plan_id']}/approve",
        headers={**auth_headers, "Idempotency-Key": "wrong-hash-key"},
        json={"plan_hash": "f" * 64},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PLAN_HASH_MISMATCH"
    assert source.read_bytes() == before


def test_permission_revocation_between_preview_and_approval_writes_nothing(
    client: TestClient,
    auth_headers: dict[str, str],
    app_settings: Settings,
    test_app: FastAPI,
) -> None:
    plan = _preview(client, auth_headers)
    source = app_settings.resolved_source_root() / "regulations/REG-000001.md"
    before = source.read_bytes()
    test_app.state.container.authenticator._context = AccessContext(  # type: ignore[attr-defined]  # noqa: SLF001
        subject_id="demo-editor",
        tenant_id="demo",
        readable_access=frozenset({AccessLevel.PUBLIC}),
        writable_document_ids=frozenset(),
    )

    response = _approve(client, auth_headers, plan, key="revoked-permission-key")

    assert response.status_code == 404
    assert source.read_bytes() == before


def test_approve_publishes_new_snapshot_and_is_idempotent(
    client: TestClient,
    auth_headers: dict[str, str],
    test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_snapshot = test_app.state.container.catalog.snapshot()
    old_result = old_snapshot.get(
        "REG-000001",
        access_context=AccessContext.anonymous(),
    )
    assert old_result is not None
    plan = _preview(client, auth_headers)

    first = _approve(client, auth_headers, plan)
    second = _approve(client, auth_headers, plan)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.headers["Location"].startswith("/api/v1/executions/")
    first_execution = first.json()
    assert first_execution["execution_id"] == second.json()["execution_id"]
    execution = _wait_for_execution(
        client,
        auth_headers,
        str(first_execution["execution_id"]),
    )
    assert execution["status"] == "completed"
    assert execution["file_status"] == "applied"
    assert execution["sync_status"] == "published"
    assert execution["graph_version_after"] != execution["graph_version_before"]
    assert execution["terminal"] is True
    assert execution["recommended_poll_after_ms"] is None

    new_snapshot = test_app.state.container.catalog.snapshot()
    access = AccessContext.anonymous()
    assert "1년" in old_result.evidence[0].quote
    assert old_snapshot.get("REG-000001", access_context=access) is None
    assert "3년" in new_snapshot.get("REG-000001", access_context=access).evidence[0].quote  # type: ignore[union-attr]
    package_root = new_snapshot.package_root
    index_paths = {
        "indexes/fts/documents.jsonl",
        "indexes/vector/embeddings.jsonl",
        "indexes/graph/edges.jsonl",
        "indexes/index-meta.json",
    }
    assert all((package_root / relative).is_file() for relative in index_paths)
    checksums = (package_root / "checksums.sha256").read_text(encoding="utf-8")
    assert all(f"  {relative}\n" in checksums for relative in index_paths)
    chunks = [
        json.loads(line)
        for line in (package_root / "retrieval/chunks.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    links = [
        json.loads(line)
        for line in (package_root / "retrieval/links.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    chunk_ids = {chunk["chunk_id"] for chunk in chunks}
    assert "REG-000001@2#sec-004" in chunk_ids
    assert all(set(link["evidence_chunk_ids"]).issubset(chunk_ids) for link in links)
    assert client.get("/api/v1/health").json()["index_artifacts_available"] is True

    def reject_index_reread(_: str) -> object:
        raise AssertionError("immutable index readiness must be cached after load")

    monkeypatch.setattr(new_snapshot, "_read_jsonl", reject_index_reread)
    assert new_snapshot.index_artifacts_available is True


def test_idempotency_key_cannot_be_reused_with_different_body(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    plan = _preview(client, auth_headers)
    first = _approve(client, auth_headers, plan, key="same-key-0000001")
    assert first.status_code == 202

    response = client.post(
        f"/api/v1/change-plans/{plan['change_plan_id']}/approve",
        headers={**auth_headers, "Idempotency-Key": "same-key-0000001"},
        json={"plan_hash": "a" * 64},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_reject_is_terminal_and_writes_nothing(
    client: TestClient,
    auth_headers: dict[str, str],
    app_settings: Settings,
) -> None:
    plan = _preview(client, auth_headers)
    source = app_settings.resolved_source_root() / "regulations/REG-000001.md"
    before = source.read_bytes()

    rejected = client.post(
        f"/api/v1/change-plans/{plan['change_plan_id']}/reject",
        headers={**auth_headers, "Idempotency-Key": "reject-key-00001"},
        json={"plan_hash": plan["plan_hash"], "reason": "요구사항 재검토"},
    )
    approved = _approve(client, auth_headers, plan, key="after-reject-key")

    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert approved.status_code == 409
    assert source.read_bytes() == before


def test_stale_plan_does_not_overwrite_external_change(
    client: TestClient,
    auth_headers: dict[str, str],
    app_settings: Settings,
) -> None:
    plan = _preview(client, auth_headers)
    source = app_settings.resolved_source_root() / "regulations/REG-000001.md"
    external = source.read_text(encoding="utf-8").replace("1년", "2년")
    source.write_text(external, encoding="utf-8")

    response = _approve(client, auth_headers, plan)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SOURCE_HASH_CONFLICT"
    assert "2년" in source.read_text(encoding="utf-8")


def test_undo_restores_exact_bytes_and_publishes_another_version(
    client: TestClient,
    auth_headers: dict[str, str],
    app_settings: Settings,
) -> None:
    source = app_settings.resolved_source_root() / "regulations/REG-000001.md"
    original = source.read_bytes()
    plan = _preview(client, auth_headers)
    approved = _approve(client, auth_headers, plan)
    approved_execution = approved.json()
    execution = _wait_for_execution(
        client,
        auth_headers,
        str(approved_execution["execution_id"]),
    )

    undone = client.post(
        f"/api/v1/executions/{execution['execution_id']}/undo",
        headers={**auth_headers, "Idempotency-Key": "undo-key-0000001"},
    )

    assert undone.status_code == 202
    pending_undo = undone.json()
    undo_execution = _wait_for_execution(
        client,
        auth_headers,
        str(pending_undo["execution_id"]),
    )
    assert undo_execution["status"] == "completed"
    assert undo_execution["undo_of_execution_id"] == execution["execution_id"]
    assert undo_execution["graph_version_after"] != execution["graph_version_after"]
    assert source.read_bytes() == original

    original_status = client.get(
        f"/api/v1/executions/{execution['execution_id']}",
        headers=auth_headers,
    )
    assert original_status.json()["status"] == "undone"


def test_demo_reset_restores_seed_source_and_version(
    client: TestClient,
    auth_headers: dict[str, str],
    app_settings: Settings,
) -> None:
    plan = _preview(client, auth_headers)
    approved = _approve(client, auth_headers, plan)
    execution = approved.json()
    assert (
        _wait_for_execution(client, auth_headers, str(execution["execution_id"]))["status"]
        == "completed"
    )

    reset = client.post("/api/v1/demo/reset", headers=auth_headers)

    assert reset.status_code == 200
    assert reset.json() == {"status": "reset", "knowledge_version": "2026-07-21.2-demo"}
    source = app_settings.resolved_source_root() / "regulations/REG-000001.md"
    assert "1년" in source.read_text(encoding="utf-8")


def test_authenticated_source_sync_uses_the_same_publish_pipeline(
    client: TestClient,
    auth_headers: dict[str, str],
    app_settings: Settings,
) -> None:
    source = app_settings.resolved_source_root() / "regulations/REG-000001.md"
    original = source.read_text(encoding="utf-8")
    updated = original.replace("1년", "3년")
    body = {
        "base_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "content": updated,
        "operation": {
            "type": "replace_exact",
            "expected_text": "1년",
            "replacement_text": "3년",
            "expected_occurrences": 1,
        },
    }
    headers = {**auth_headers, "Idempotency-Key": "watchdog-sync-key"}

    first = client.post(
        "/api/v1/documents/REG-000001/source-sync-plans",
        headers=headers,
        json=body,
    )

    assert first.status_code == 201
    assert source.read_text(encoding="utf-8") == original
    plan = first.json()
    approved = client.post(
        f"/api/v1/change-plans/{plan['change_plan_id']}/approve",
        headers=headers,
        json={"plan_hash": plan["plan_hash"]},
    )
    assert approved.status_code == 202
    execution = _wait_for_execution(
        client,
        auth_headers,
        str(approved.json()["execution_id"]),
    )
    assert execution["status"] == "completed"
    assert source.read_text(encoding="utf-8") == updated

    replay = client.post(
        "/api/v1/documents/REG-000001/source-sync-plans",
        headers=headers,
        json=body,
    )
    assert replay.status_code == 201
    assert replay.json()["change_plan_id"] == plan["change_plan_id"]

    conflict = client.post(
        "/api/v1/documents/REG-000001/source-sync-plans",
        headers=headers,
        json={**body, "content": updated.replace("3년", "5년")},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_heading_change_remains_citable_and_publishable(
    client: TestClient,
    auth_headers: dict[str, str],
    app_settings: Settings,
    test_app: FastAPI,
) -> None:
    plan = _preview(
        client,
        auth_headers,
        old="제4조 보관 기간",
        new="제4조 저장 기간",
        conversation_id="heading-change",
    )
    approved = _approve(client, auth_headers, plan, key="heading-change-key")
    execution = _wait_for_execution(
        client,
        auth_headers,
        str(approved.json()["execution_id"]),
    )

    assert execution["status"] == "completed"
    source = app_settings.resolved_source_root() / "regulations/REG-000001.md"
    assert "제4조 저장 기간" in source.read_text(encoding="utf-8")
    result = test_app.state.container.catalog.snapshot().get(
        "REG-000001",
        access_context=AccessContext.anonymous(),
    )
    assert result is not None
    assert result.evidence[0].section == "제4조 저장 기간"
    assert result.evidence[0].chunk_id == "REG-000001@2#sec-004"


def test_source_sync_rejects_change_the_local_evidence_adapter_cannot_represent(
    client: TestClient,
    auth_headers: dict[str, str],
    app_settings: Settings,
) -> None:
    source = app_settings.resolved_source_root() / "regulations/REG-000001.md"
    original = source.read_text(encoding="utf-8")
    old = "# 개인정보 처리 규정\n\n## 제4조 보관 기간"
    new = "# 개인정보 보호 규정\n\n## 제4조 저장 기간"

    response = client.post(
        "/api/v1/documents/REG-000001/source-sync-plans",
        headers={**auth_headers, "Idempotency-Key": "unindexable-sync-key"},
        json={
            "base_sha256": hashlib.sha256(original.encode()).hexdigest(),
            "content": original.replace(old, new),
            "operation": {
                "type": "replace_exact",
                "expected_text": old,
                "replacement_text": new,
                "expected_occurrences": 1,
            },
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CHANGE_NOT_INDEXABLE"
    assert source.read_text(encoding="utf-8") == original


def test_change_plan_rejects_unverified_pdf_write_format(tmp_path: Path) -> None:
    row = json.loads(
        Path("demo/llm-wiki/manifest.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    row["source"].update(
        {
            "filename": "regulations/REG-000001.pdf",
            "uri": "source://regulations/REG-000001.pdf",
            "media_type": "application/pdf",
        }
    )
    manifest = ManifestEntry.model_validate(row)

    class FakeRepository:
        @staticmethod
        def get_manifest(*_: object, **__: object) -> ManifestEntry:
            return manifest

        @staticmethod
        def supports_exact_change(*_: object, **__: object) -> bool:
            return True

    source_root = tmp_path / "source"
    source_root.mkdir()
    state = StateStore(tmp_path / "state.sqlite3")
    state.initialize()
    plans = ChangePlanService(
        settings=Settings(),
        file_store=SafeSourceFileStore(
            SourceUriResolver(source_root),
            backup_root=tmp_path / "backups",
            max_bytes=1024,
        ),
        state_store=state,
    )
    context = AccessContext(
        subject_id="user-1",
        tenant_id="tenant-1",
        readable_access=frozenset({AccessLevel.PUBLIC}),
        writable_document_ids=frozenset({"REG-000001"}),
        provisioned=True,
    )

    with pytest.raises(ChangeServiceError) as raised:
        plans.create_plan(
            repository=FakeRepository(),  # type: ignore[arg-type]
            access_context=context,
            document_id="REG-000001",
            operation=ReplaceExactOperation(
                expected_text="old",
                replacement_text="new",
            ),
            request_text="PDF 수정",
        )

    assert raised.value.code == "UNSUPPORTED_WRITE_FORMAT"


def test_undo_conflicts_after_external_edit(
    client: TestClient,
    auth_headers: dict[str, str],
    app_settings: Settings,
) -> None:
    plan = _preview(client, auth_headers)
    pending = _approve(client, auth_headers, plan).json()
    approved = _wait_for_execution(
        client,
        auth_headers,
        str(pending["execution_id"]),
    )
    source = app_settings.resolved_source_root() / "regulations/REG-000001.md"
    source.write_text(source.read_text(encoding="utf-8") + "\n외부 수정\n", encoding="utf-8")

    response = client.post(
        f"/api/v1/executions/{approved['execution_id']}/undo",
        headers={**auth_headers, "Idempotency-Key": "undo-conflict-key"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "UNDO_HASH_CONFLICT"
    assert "외부 수정" in source.read_text(encoding="utf-8")


def test_two_plans_from_same_base_allow_only_one_execution(
    client: TestClient,
    auth_headers: dict[str, str],
    test_app: FastAPI,
) -> None:
    first = _preview(client, auth_headers, new="3년", conversation_id="concurrent-a")
    second = _preview(client, auth_headers, new="5년", conversation_id="concurrent-b")
    context = authenticate_authorization_header(
        auth_headers["Authorization"],
        test_app.state.container.authenticator,
    )

    async def execute_both() -> list[object]:
        async def approve(plan: dict[str, object], key: str) -> object:
            try:
                return await test_app.state.container.executions.approve(
                    plan_id=str(plan["change_plan_id"]),
                    supplied_plan_hash=str(plan["plan_hash"]),
                    access_context=context,
                    idempotency_key=key,
                )
            except ChangeServiceError as error:
                return error

        return await asyncio.gather(
            approve(first, "concurrent-key-a"),
            approve(second, "concurrent-key-b"),
        )

    results = anyio.run(execute_both)
    completed = [item for item in results if not isinstance(item, ChangeServiceError)]
    conflicts = [item for item in results if isinstance(item, ChangeServiceError)]
    assert len(completed) == 1
    assert len(conflicts) == 1
    assert conflicts[0].code == "SOURCE_HASH_CONFLICT"
