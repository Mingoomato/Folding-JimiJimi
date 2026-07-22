# mypy: disable-error-code="no-untyped-call"
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from codegate_api.documents.models import DocumentFormat
from codegate_api.documents.writers.base import WriterError


@dataclass(frozen=True, slots=True)
class RenderedPage:
    label: str
    png: bytes


class ManagedDocumentRenderer:
    def __init__(self, *, libreoffice_bin: Path | None, timeout_seconds: float) -> None:
        self._libreoffice_bin = libreoffice_bin
        self._timeout_seconds = timeout_seconds

    @property
    def libreoffice_available(self) -> bool:
        return self._libreoffice_bin is not None

    @property
    def fingerprint(self) -> str:
        return "PyMuPDF/" + pymupdf.VersionBind

    def render(
        self,
        *,
        content: bytes,
        format_: DocumentFormat,
        job_root: Path,
        selected_pages: set[int] | None = None,
        limit: int = 20,
    ) -> tuple[list[RenderedPage], int]:
        job_root.mkdir(parents=True, exist_ok=True)
        if format_ is DocumentFormat.PDF:
            pdf = content
        elif format_ in {DocumentFormat.DOCX, DocumentFormat.PPTX, DocumentFormat.XLSX}:
            pdf = self.convert_to_pdf(content=content, format_=format_, job_root=job_root)
        else:
            raise WriterError(
                "renderer_unavailable",
                f"managed renderer does not support {format_.value}",
                retryable=True,
            )
        try:
            document = pymupdf.open(stream=pdf, filetype="pdf")
        except Exception as error:
            raise WriterError("malformed_render", "renderer produced an invalid PDF") from error
        try:
            indices = list(range(len(document)))
            if selected_pages is not None:
                indices = [index for index in indices if index in selected_pages]
            truncated = max(0, len(indices) - limit)
            pages = []
            for index in indices[:limit]:
                pixmap = document[index].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False)
                pages.append(RenderedPage(label=f"page-{index + 1}", png=pixmap.tobytes("png")))
            return pages, truncated
        finally:
            document.close()

    def convert_to_pdf(
        self,
        *,
        content: bytes,
        format_: DocumentFormat,
        job_root: Path,
    ) -> bytes:
        if self._libreoffice_bin is None:
            raise WriterError(
                "renderer_unavailable",
                "managed LibreOffice is unavailable",
                retryable=True,
            )
        if format_ not in {DocumentFormat.DOCX, DocumentFormat.PPTX, DocumentFormat.XLSX}:
            raise WriterError("unsupported_operation", "LibreOffice input format is unsupported")
        conversion_root = job_root / f"lo-{uuid.uuid4().hex}"
        profile = conversion_root / "profile"
        output = conversion_root / "output"
        conversion_root.mkdir(parents=True, exist_ok=False)
        profile.mkdir()
        output.mkdir()
        input_path = conversion_root / f"input.{format_.value}"
        input_path.write_bytes(content)
        profile_uri = profile.resolve().as_uri()
        argv = [
            str(self._libreoffice_bin),
            "--headless",
            "--nologo",
            "--nodefault",
            "--nofirststartwizard",
            "--nolockcheck",
            f"-env:UserInstallation={profile_uri}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(output),
            str(input_path),
        ]
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
        try:
            stdout, stderr = process.communicate(timeout=self._timeout_seconds)
        except subprocess.TimeoutExpired as error:
            _terminate_process_tree(process)
            raise WriterError(
                "renderer_timeout",
                "LibreOffice conversion timed out",
                retryable=True,
            ) from error
        if process.returncode != 0:
            message = (stderr or stdout).decode("utf-8", errors="replace").strip()[:500]
            raise WriterError(
                "renderer_crash",
                "LibreOffice conversion failed" + (f": {message}" if message else ""),
                retryable=True,
            )
        pdf_path = output / "input.pdf"
        if not pdf_path.is_file():
            raise WriterError(
                "renderer_malformed_output",
                "LibreOffice did not produce the expected PDF",
                retryable=True,
            )
        result = pdf_path.read_bytes()
        try:
            check = pymupdf.open(stream=result, filetype="pdf")
            check.close()
        except Exception as error:
            raise WriterError(
                "renderer_malformed_output",
                "LibreOffice produced an invalid PDF",
                retryable=True,
            ) from error
        return result


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill:
            subprocess.run(
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            process.kill()
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
