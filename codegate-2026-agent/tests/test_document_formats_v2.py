from __future__ import annotations

import hashlib
import io
import json
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pymupdf
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from codegate_api.config import Settings
from codegate_api.documents.capabilities import DocumentCapabilityRegistry
from codegate_api.documents.models import (
    CapabilityRegistryView,
    CapabilityStatus,
    DocumentCreationPlanRequest,
    DocumentFormat,
    FormulaValue,
    MarkdownCreationPayload,
    NativeLocator,
    PdfAnnotationAddOperation,
    PdfRedactTextOperation,
    SpreadsheetCellSet,
    SpreadsheetCellsSetOperation,
    StringValue,
    TextReplaceOperation,
    WorkbookCell,
    WorkbookCreationPayload,
    WorkbookSheet,
)
from codegate_api.documents.rendering import RenderedPage
from codegate_api.documents.security import DocumentPackageGuard, DocumentPolicyError
from codegate_api.documents.templates import DocumentTemplateRegistry, TemplateRegistryError
from codegate_api.documents.writers.docx import DocxWriter
from codegate_api.documents.writers.pdf import PdfWriter
from codegate_api.documents.writers.pptx import PptxWriter
from codegate_api.documents.writers.xlsx import XlsxWriter
from codegate_api.main import create_app


def test_docx_create_mutate_and_parse_after_write() -> None:
    writer = DocxWriter()
    created = writer.create(MarkdownCreationPayload(markdown="# Quarterly report\nOld wording"))
    target = next(
        item for item in writer.read_structure(created.content) if item.value == "Old wording"
    )

    proposed = writer.mutate(
        created.content,
        [
            TextReplaceOperation(
                locator=target.locator,
                expected="Old wording",
                replacement="Approved wording",
            )
        ],
    )

    assert any(item.value == "Approved wording" for item in writer.read_structure(proposed.content))
    assert proposed.structural_diff[0].before == "Old wording"


def test_approved_docx_template_is_hash_pinned_and_applied(tmp_path: Path) -> None:
    template_root = tmp_path / "templates"
    template_root.mkdir()
    template = DocxWriter().create(MarkdownCreationPayload(markdown="# Company template"))
    template_path = template_root / "company.docx"
    template_path.write_bytes(template.content)
    digest = hashlib.sha256(template.content).hexdigest()
    (template_root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "templates": [
                    {
                        "template_id": "company-default",
                        "format": "docx",
                        "relative_path": "company.docx",
                        "sha256": digest,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    approved = DocumentTemplateRegistry(template_root, max_bytes=1_048_576).get(
        "company-default",
        format_=DocumentFormat.DOCX,
    )

    proposed = DocxWriter().create(
        MarkdownCreationPayload(markdown="Approved body"),
        template=approved.content,
    )
    values = [item.value for item in DocxWriter().read_structure(proposed.content)]

    assert "Company template" in values
    assert "Approved body" in values


def test_approved_template_hash_change_fails_closed(tmp_path: Path) -> None:
    template_root = tmp_path / "templates"
    template_root.mkdir()
    (template_root / "company.docx").write_bytes(b"changed")
    (template_root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "templates": [
                    {
                        "template_id": "company-default",
                        "format": "docx",
                        "relative_path": "company.docx",
                        "sha256": "0" * 64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(TemplateRegistryError, match="no longer match"):
        DocumentTemplateRegistry(template_root, max_bytes=1_048_576).get(
            "company-default",
            format_=DocumentFormat.DOCX,
        )


def test_pptx_create_mutate_and_parse_after_write() -> None:
    writer = PptxWriter()
    created = writer.create(MarkdownCreationPayload(markdown="# Summary\nOld slide text"))
    target = next(
        item for item in writer.read_structure(created.content) if item.value == "Old slide text"
    )

    proposed = writer.mutate(
        created.content,
        [
            TextReplaceOperation(
                locator=target.locator,
                expected="Old slide text",
                replacement="New slide text",
            )
        ],
    )

    assert any(item.value == "New slide text" for item in writer.read_structure(proposed.content))


def test_xlsx_keeps_strings_and_formulas_as_distinct_typed_values() -> None:
    writer = XlsxWriter()
    created = writer.create(
        WorkbookCreationPayload(
            sheets=[
                WorkbookSheet(
                    name="Data",
                    cells=[
                        WorkbookCell(address="A1", value=StringValue(value="=literal")),
                        WorkbookCell(address="B1", value=FormulaValue(value="=1+1")),
                    ],
                )
            ]
        )
    )
    structure = writer.read_structure(created.content)
    literal = next(item for item in structure if item.locator.address == "A1")
    formula = next(item for item in structure if item.locator.address == "B1")
    assert literal.value == {"type": "string", "value": "=literal"}
    assert formula.value == {"type": "formula", "value": "=1+1"}

    proposed = writer.mutate(
        created.content,
        [
            SpreadsheetCellsSetOperation(
                cells=[
                    SpreadsheetCellSet(
                        sheet_name="Data",
                        address="A1",
                        expected=StringValue(value="=literal"),
                        replacement=StringValue(value="approved"),
                    )
                ]
            )
        ],
    )
    updated = next(
        item for item in writer.read_structure(proposed.content) if item.locator.address == "A1"
    )
    assert updated.value == {"type": "string", "value": "approved"}


def test_pdf_annotation_and_exact_text_redaction_round_trip() -> None:
    source_document = pymupdf.open()
    page = source_document.new_page()
    page.insert_text((72, 72), "Secret phrase")
    source = source_document.tobytes()
    source_document.close()
    writer = PdfWriter()

    annotated = writer.mutate(
        source,
        [
            PdfAnnotationAddOperation(
                locator=NativeLocator(kind="pdf_page", page_index=0),
                text="Reviewer note",
                rect=(72, 90, 260, 140),
            )
        ],
    )
    redacted = writer.mutate(
        annotated.content,
        [
            PdfRedactTextOperation(
                locator=NativeLocator(kind="pdf_page", page_index=0),
                expected="Secret phrase",
            )
        ],
    )

    parsed = pymupdf.open(stream=redacted.content, filetype="pdf")
    try:
        assert "Secret phrase" not in parsed[0].get_text("text")
        assert next(parsed[0].annots()).info["content"] == "Reviewer note"
    finally:
        parsed.close()


def test_package_guard_rejects_active_content_fail_closed() -> None:
    source = DocxWriter().create(MarkdownCreationPayload(markdown="# Safe")).content
    input_archive = zipfile.ZipFile(io.BytesIO(source))
    output = io.BytesIO()
    with input_archive, zipfile.ZipFile(output, "w") as modified:
        for info in input_archive.infolist():
            modified.writestr(info, input_archive.read(info))
        modified.writestr("word/vbaProject.bin", b"macro")
    guard = DocumentPackageGuard(
        max_source_bytes=10_000_000,
        max_result_bytes=10_000_000,
        max_parts=1_000,
        max_expanded_bytes=10_000_000,
    )

    with pytest.raises(DocumentPolicyError, match="macros") as caught:
        guard.inspect_source(output.getvalue(), DocumentFormat.DOCX)

    assert caught.value.code == "unsupported_active_content"


@pytest.mark.parametrize(
    "target",
    ["C:/outside/report.docx", "/outside/report.docx", "safe/../outside/report.docx"],
)
def test_creation_request_rejects_unconfined_target(target: str) -> None:
    with pytest.raises(ValidationError):
        DocumentCreationPlanRequest.model_validate(
            {
                "document_id": "DOC-000001",
                "format": "docx",
                "capability_id": "docx.writer.python-docx/v1",
                "capability_snapshot_id": "0" * 64,
                "graph_version": "graph-1",
                "target_relative_path": target,
                "payload": {
                    "type": "document.create_from_markdown/v1",
                    "markdown": "# Document",
                },
            }
        )


def test_v2_capabilities_and_post_idempotency_contract(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    response = client.get("/api/v2/document-capabilities", headers=auth_headers)
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["snapshot_id"]) == 64
    assert {item["format"] for item in payload["capabilities"]} == {
        "hwp",
        "hwpx",
        "docx",
        "pptx",
        "xlsx",
        "pdf",
    }


def test_v2_chat_requires_idempotency_and_replays_typed_response(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    body = {
        "conversation_id": "native-doc-chat",
        "message": "REG-000001 document location",
    }
    missing = client.post("/api/v2/chat/messages", headers=auth_headers, json=body)
    assert missing.status_code == 422

    headers = {**auth_headers, "Idempotency-Key": "native-chat-replay"}
    first = client.post("/api/v2/chat/messages", headers=headers, json=body)
    replay = client.post("/api/v2/chat/messages", headers=headers, json=body)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert set(first.json()) == {
        "conversation_id",
        "message_id",
        "response_type",
        "assistant_text",
        "documents",
        "change_plan",
        "document_plan",
        "error",
    }

    payload = client.get("/api/v2/document-capabilities", headers=auth_headers).json()
    missing_key = client.post(
        "/api/v2/document-creation-plans",
        headers=auth_headers,
        json={
            "document_id": "MAN-000001",
            "format": "docx",
            "capability_id": "docx.writer.python-docx/v1",
            "capability_snapshot_id": payload["snapshot_id"],
            "graph_version": "2026-07-21.2-demo",
            "target_relative_path": "created/report.docx",
            "payload": {
                "type": "document.create_from_markdown/v1",
                "markdown": "# Report",
            },
        },
    )
    assert missing_key.status_code == 422


def test_pdf_mutation_stays_active_without_libreoffice_but_creation_is_disabled(
    app_settings: Settings,
) -> None:
    registry = DocumentCapabilityRegistry(app_settings)
    registry._libreoffice_bin = None  # noqa: SLF001

    capability = registry.get("pdf.writer.pymupdf/v1")

    assert capability.active is True
    assert capability.mutate is True
    assert capability.render is True
    assert capability.create is False


def _install_test_docx_runtime(app: Any) -> tuple[CapabilityStatus, CapabilityRegistryView]:
    capability = CapabilityStatus(
        capability_id="docx.writer.python-docx/v1",
        format=DocumentFormat.DOCX,
        operations=["document.create_from_markdown/v1"],
        read=True,
        create=True,
        mutate=True,
        render=True,
        active=True,
        disabled_reasons=[],
        writer_name="python-docx",
        writer_version="1.2.0",
        renderer_name="test-renderer",
        renderer_version="1.0.0",
        max_source_bytes=10_000_000,
        max_result_bytes=10_000_000,
        signed_policy="reject-mutation",
        macro_policy="read-only",
    )
    snapshot = CapabilityRegistryView(
        snapshot_id="1" * 64,
        generated_at=datetime.now(UTC),
        capabilities=[capability],
    )

    class Capabilities:
        def snapshot(self) -> CapabilityRegistryView:
            return snapshot

        def require(self, **_values: Any) -> CapabilityStatus:
            return capability

    class Renderer:
        libreoffice_available = True
        fingerprint = "test-renderer/1.0.0"

        def render(self, **_values: Any) -> tuple[list[RenderedPage], int]:
            return [RenderedPage(label="page-1", png=b"test-png")], 0

    service = app.state.container.document_service
    service._capabilities = Capabilities()  # type: ignore[assignment]
    service._renderer = Renderer()  # type: ignore[assignment]
    return capability, snapshot


def test_v2_creation_approval_and_recovery_undo_end_to_end(
    client: TestClient,
    auth_headers: dict[str, str],
    app_settings: Settings,
) -> None:
    capability, snapshot = _install_test_docx_runtime(client.app)

    created = client.post(
        "/api/v2/document-creation-plans",
        headers={**auth_headers, "Idempotency-Key": "create-docx-e2e-0001"},
        json={
            "document_id": "MAN-000001",
            "format": "docx",
            "capability_id": capability.capability_id,
            "capability_snapshot_id": snapshot.snapshot_id,
            "graph_version": "2026-07-21.2-demo",
            "target_relative_path": "created/report.docx",
            "payload": {
                "type": "document.create_from_markdown/v1",
                "markdown": "# Approved report\nCreated through the v2 approval flow.",
            },
        },
    )
    assert created.status_code == 202
    plan = created.json()
    for _ in range(100):
        current = client.get(
            f"/api/v2/change-plans/{plan['change_plan_id']}",
            headers=auth_headers,
        ).json()
        if current["status"] != "preparing":
            break
        time.sleep(0.02)
    assert current["status"] == "pending_approval"
    assert current["preview_manifest"]["pairs"][0]["after_artifact_id"]

    approved = client.post(
        f"/api/v2/change-plans/{plan['change_plan_id']}/approve",
        headers={**auth_headers, "Idempotency-Key": "approve-docx-e2e-0001"},
        json={"plan_hash": current["plan_hash"]},
    )
    assert approved.status_code == 202
    execution = approved.json()
    target = app_settings.resolved_source_root() / "created/report.docx"
    assert target.is_file()
    assert execution["status"] in {"file_applied", "syncing", "sync_failed", "completed"}

    for _ in range(100):
        execution = client.get(
            f"/api/v2/executions/{execution['execution_id']}",
            headers=auth_headers,
        ).json()
        if execution["status"] in {"sync_failed", "completed"}:
            break
        time.sleep(0.02)
    assert execution["status"] in {"sync_failed", "completed"}

    undone = client.post(
        f"/api/v2/executions/{execution['execution_id']}/undo",
        headers={**auth_headers, "Idempotency-Key": "undo-docx-e2e-0001"},
    )
    assert undone.status_code == 202
    undo_execution = undone.json()
    assert not target.exists()
    recovery = (
        app_settings.resolved_document_recovery_root()
        / undo_execution["execution_id"]
        / "report.docx"
    )
    assert recovery.is_file()


def test_v2_creation_recovers_crash_after_create_new(
    app_settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    first_app = create_app(app_settings)
    with TestClient(first_app) as first_client:
        capability, snapshot = _install_test_docx_runtime(first_app)
        created = first_client.post(
            "/api/v2/document-creation-plans",
            headers={**auth_headers, "Idempotency-Key": "create-crash-v2-0001"},
            json={
                "document_id": "MAN-000002",
                "format": "docx",
                "capability_id": capability.capability_id,
                "capability_snapshot_id": snapshot.snapshot_id,
                "graph_version": "2026-07-21.2-demo",
                "target_relative_path": "created/crash-recovery.docx",
                "payload": {
                    "type": "document.create_from_markdown/v1",
                    "markdown": "# Crash recovery\nApproved artifact.",
                },
            },
        )
        assert created.status_code == 202
        plan_id = created.json()["change_plan_id"]
        for _ in range(100):
            plan = first_client.get(f"/api/v2/change-plans/{plan_id}", headers=auth_headers).json()
            if plan["status"] != "preparing":
                break
            time.sleep(0.02)
        assert plan["status"] == "pending_approval"

        def crash_after_create(stage: str) -> None:
            if stage == "created":
                raise RuntimeError("simulated create crash")

        first_app.state.container.document_files._fault_hook = (  # noqa: SLF001
            crash_after_create
        )
        with pytest.raises(RuntimeError, match="simulated create crash"):
            first_client.post(
                f"/api/v2/change-plans/{plan_id}/approve",
                headers={**auth_headers, "Idempotency-Key": "approve-crash-v2-0001"},
                json={"plan_hash": plan["plan_hash"]},
            )
        target = app_settings.resolved_source_root() / "created/crash-recovery.docx"
        assert target.is_file()

    recovered_app = create_app(app_settings)
    with TestClient(recovered_app) as recovered_client:
        replay = recovered_client.post(
            f"/api/v2/change-plans/{plan_id}/approve",
            headers={**auth_headers, "Idempotency-Key": "approve-crash-v2-0001"},
            json={"plan_hash": plan["plan_hash"]},
        )
        assert replay.status_code == 202
        execution = replay.json()
        assert execution["status"] in {"file_applied", "syncing", "sync_failed", "completed"}
        assert target.read_bytes()
