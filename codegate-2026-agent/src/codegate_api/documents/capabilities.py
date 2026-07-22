from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from codegate_api.config import Settings
from codegate_api.documents.models import (
    CapabilityRegistryView,
    CapabilityStatus,
    DocumentFormat,
)

CAPABILITY_IDS = {
    DocumentFormat.HWP: "hwp.derive.kordoc/v1",
    DocumentFormat.HWPX: "hwpx.writer.kordoc/v1",
    DocumentFormat.DOCX: "docx.writer.python-docx/v1",
    DocumentFormat.PPTX: "pptx.writer.python-pptx/v1",
    DocumentFormat.XLSX: "xlsx.writer.openpyxl/v1",
    DocumentFormat.PDF: "pdf.writer.pymupdf/v1",
}


OPERATIONS: dict[DocumentFormat, list[str]] = {
    DocumentFormat.HWP: ["hwp.derive_hwpx/v1"],
    DocumentFormat.HWPX: [
        "text.replace/v1",
        "table.cell_set/v1",
        "document.create_from_markdown/v1",
    ],
    DocumentFormat.DOCX: [
        "text.replace/v1",
        "table.cell_set/v1",
        "document.create_from_markdown/v1",
    ],
    DocumentFormat.PPTX: [
        "text.replace/v1",
        "table.cell_set/v1",
        "document.create_from_markdown/v1",
    ],
    DocumentFormat.XLSX: ["spreadsheet.cells_set/v1", "workbook.create/v1"],
    DocumentFormat.PDF: [
        "pdf.annotation_add/v1",
        "pdf.form_field_set/v1",
        "pdf.redact_text/v1",
        "document.create_from_markdown/v1",
    ],
}


class DocumentCapabilityRegistry:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._libreoffice_bin = _find_libreoffice(settings.resolved_libreoffice_bin())
        self._node_bin = _find_node(settings.resolved_node_bin())
        self._kordoc_root = _find_kordoc_root(settings.resolved_kordoc_workspace_root())

    @property
    def libreoffice_bin(self) -> Path | None:
        return self._libreoffice_bin

    @property
    def node_bin(self) -> Path | None:
        return self._node_bin

    @property
    def kordoc_root(self) -> Path | None:
        return self._kordoc_root

    def snapshot(self) -> CapabilityRegistryView:
        capabilities = [self._capability(format_) for format_ in DocumentFormat]
        canonical = json.dumps(
            [item.model_dump(mode="json") for item in capabilities],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return CapabilityRegistryView(
            snapshot_id=hashlib.sha256(canonical).hexdigest(),
            generated_at=datetime.now(UTC),
            capabilities=capabilities,
        )

    def get(self, capability_id: str) -> CapabilityStatus:
        for capability in self.snapshot().capabilities:
            if capability.capability_id == capability_id:
                return capability
        raise KeyError(capability_id)

    def require(
        self,
        *,
        capability_id: str,
        format_: DocumentFormat,
        operation_types: Sequence[str],
        snapshot_id: str,
    ) -> CapabilityStatus:
        snapshot = self.snapshot()
        if snapshot.snapshot_id != snapshot_id:
            raise CapabilityUnavailable("capability_snapshot_stale", "capabilities changed")
        try:
            capability = next(
                item for item in snapshot.capabilities if item.capability_id == capability_id
            )
        except StopIteration as error:
            raise CapabilityUnavailable(
                "capability_unknown", "unknown document capability"
            ) from error
        if capability.format is not format_:
            raise CapabilityUnavailable("capability_format_mismatch", "capability format mismatch")
        unsupported = sorted(set(operation_types) - set(capability.operations))
        if unsupported:
            raise CapabilityUnavailable(
                "unsupported_operation",
                "unsupported operations: " + ", ".join(unsupported),
            )
        if not capability.active:
            raise CapabilityUnavailable(
                "capability_unavailable",
                "; ".join(capability.disabled_reasons) or "capability is disabled",
                retryable=True,
            )
        return capability

    def _capability(self, format_: DocumentFormat) -> CapabilityStatus:
        max_source = self._settings.document_max_source_bytes
        max_result = self._settings.document_max_result_bytes
        libreoffice_version = _binary_version(self._libreoffice_bin, "--version")
        if format_ in {DocumentFormat.HWP, DocumentFormat.HWPX}:
            reasons: list[str] = []
            if self._node_bin is None:
                reasons.append("Node.js 22 is unavailable")
            if self._kordoc_root is None:
                reasons.append("Kordoc 4.2.5 is unavailable")
            return CapabilityStatus(
                capability_id=CAPABILITY_IDS[format_],
                format=format_,
                operations=OPERATIONS[format_],
                read=True,
                create=format_ is DocumentFormat.HWPX and not reasons,
                mutate=format_ is DocumentFormat.HWPX and not reasons,
                render=format_ is DocumentFormat.HWPX and not reasons,
                active=not reasons,
                disabled_reasons=reasons,
                writer_name="Kordoc",
                writer_version="4.2.5" if not reasons else None,
                renderer_name="Kordoc",
                renderer_version="4.2.5" if not reasons else None,
                max_source_bytes=max_source,
                max_result_bytes=max_result,
                signed_policy="reject-mutation",
                macro_policy="read-only",
            )
        package = {
            DocumentFormat.DOCX: ("python-docx", "python-docx"),
            DocumentFormat.PPTX: ("python-pptx", "python-pptx"),
            DocumentFormat.XLSX: ("openpyxl", "openpyxl"),
            DocumentFormat.PDF: ("PyMuPDF", "PyMuPDF"),
        }[format_]
        version = _package_version(package[0])
        reasons = [] if version else [f"{package[1]} is unavailable"]
        renderer_name = "PyMuPDF" if format_ is DocumentFormat.PDF else "LibreOffice"
        renderer_version = version if format_ is DocumentFormat.PDF else libreoffice_version
        if format_ is not DocumentFormat.PDF and self._libreoffice_bin is None:
            reasons.append("managed LibreOffice is unavailable")
        active = not reasons
        can_create = active and (
            format_ is not DocumentFormat.PDF or self._libreoffice_bin is not None
        )
        return CapabilityStatus(
            capability_id=CAPABILITY_IDS[format_],
            format=format_,
            operations=OPERATIONS[format_],
            read=True,
            create=can_create,
            mutate=active,
            render=active,
            active=active,
            disabled_reasons=reasons,
            writer_name=package[1],
            writer_version=version,
            renderer_name=renderer_name,
            renderer_version=renderer_version,
            max_source_bytes=max_source,
            max_result_bytes=max_result,
            signed_policy="reject-mutation",
            macro_policy=("not-applicable" if format_ is DocumentFormat.PDF else "read-only"),
        )


class CapabilityUnavailable(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _find_node(configured: Path | None) -> Path | None:
    if configured is not None:
        return configured if configured.is_file() else None
    found = shutil.which("node")
    if not found:
        return None
    path = Path(found).resolve()
    version = _binary_version(path, "--version")
    if version is None or not version.lstrip("v").startswith("22."):
        return None
    return path


def _find_kordoc_root(configured: Path | None) -> Path | None:
    candidates = [configured] if configured is not None else []
    candidates.extend(
        [
            Path.cwd() / "../codegate-2026-local",
            Path.cwd() / "codegate-2026-local",
        ]
    )
    for candidate in candidates:
        if candidate is None:
            continue
        resolved = candidate.resolve()
        package_json = resolved / "node_modules/kordoc/package.json"
        if package_json.is_file():
            try:
                package = json.loads(package_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if package.get("version") == "4.2.5":
                return resolved
    return None


def _find_libreoffice(configured: Path | None) -> Path | None:
    if configured is not None:
        return configured if configured.is_file() else None
    candidates: list[Path] = []
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        candidates.append(Path(found))
    if os.name == "nt":
        candidates.extend(
            [
                Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
                / "LibreOffice/program/soffice.exe",
                Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
                / "LibreOffice/program/soffice.exe",
            ]
        )
    else:
        candidates.extend(
            [
                Path("/usr/bin/libreoffice"),
                Path("/usr/bin/soffice"),
                Path("/opt/libreoffice/program/soffice"),
            ]
        )
    return next((path.resolve() for path in candidates if path.is_file()), None)


def _binary_version(binary: Path | None, argument: str) -> str | None:
    if binary is None:
        return None
    try:
        completed = subprocess.run(
            [str(binary), argument],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (completed.stdout or completed.stderr).strip()
    return output[:128] if completed.returncode == 0 and output else None
