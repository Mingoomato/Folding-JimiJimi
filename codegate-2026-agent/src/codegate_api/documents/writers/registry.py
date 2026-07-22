from __future__ import annotations

from pathlib import Path

from codegate_api.config import Settings
from codegate_api.documents.capabilities import DocumentCapabilityRegistry
from codegate_api.documents.kordoc import KordocWorkerClient
from codegate_api.documents.models import (
    CreationPayload,
    DocumentFormat,
    DocumentOperation,
    HwpDerivationPayload,
    MarkdownCreationPayload,
    NativeLocator,
    StructureItem,
)
from codegate_api.documents.rendering import ManagedDocumentRenderer
from codegate_api.documents.security import DocumentPackageGuard
from codegate_api.documents.templates import ApprovedTemplate
from codegate_api.documents.writers.base import DocumentWriter, ProposedDocument, WriterError
from codegate_api.documents.writers.docx import DocxWriter
from codegate_api.documents.writers.hwpx import HwpxWriter
from codegate_api.documents.writers.pdf import PdfWriter
from codegate_api.documents.writers.pptx import PptxWriter
from codegate_api.documents.writers.xlsx import XlsxWriter


class WriterRegistry:
    def __init__(
        self,
        *,
        settings: Settings,
        capabilities: DocumentCapabilityRegistry,
        renderer: ManagedDocumentRenderer,
    ) -> None:
        self._guard = DocumentPackageGuard(
            max_source_bytes=settings.document_max_source_bytes,
            max_result_bytes=settings.document_max_result_bytes,
            max_parts=settings.document_max_zip_parts,
            max_expanded_bytes=settings.document_max_zip_expanded_bytes,
        )
        self._renderer = renderer
        self._writers: dict[DocumentFormat, DocumentWriter] = {
            DocumentFormat.DOCX: DocxWriter(),
            DocumentFormat.PPTX: PptxWriter(),
            DocumentFormat.XLSX: XlsxWriter(),
            DocumentFormat.PDF: PdfWriter(),
        }
        self._kordoc_client: KordocWorkerClient | None = None
        if capabilities.node_bin and capabilities.kordoc_root:
            self._kordoc_client = KordocWorkerClient(
                node_bin=capabilities.node_bin,
                kordoc_root=capabilities.kordoc_root,
                staging_root=settings.resolved_document_artifact_root(),
                timeout_seconds=settings.document_worker_timeout_seconds,
            )
            self._writers[DocumentFormat.HWPX] = HwpxWriter(
                worker=self._kordoc_client,
                staging_root=settings.resolved_document_artifact_root(),
            )

    def close(self) -> None:
        if self._kordoc_client is not None:
            self._kordoc_client.close()

    def read_structure(self, source: bytes, format_: DocumentFormat) -> list[StructureItem]:
        if format_ is DocumentFormat.HWP:
            if self._kordoc_client is None:
                raise WriterError("writer_unavailable", "Kordoc is unavailable", retryable=True)
            # HWP source structure is read by deriving through the same isolated Kordoc parse path.
            staging = self._writers.get(DocumentFormat.HWPX)
            assert isinstance(staging, HwpxWriter)
            job = staging._job()  # noqa: SLF001 - shared writer-internal staging boundary
            input_path = job / "input.hwp"
            input_path.write_bytes(source)
            result = self._kordoc_client.request(
                "parse",
                input=self._kordoc_client.relative(input_path),
            )
            blocks = result.get("blocks", [])
            return [
                StructureItem(
                    locator=NativeLocator(kind="paragraph", block_index=index),
                    value=block,
                    value_type=(
                        str(block.get("type", "block")) if isinstance(block, dict) else "block"
                    ),
                )
                for index, block in enumerate(blocks if isinstance(blocks, list) else [])
            ]
        writer = self._require_writer(format_)
        self._guard.inspect_source(source, format_)
        return writer.read_structure(source)

    def mutate(
        self,
        *,
        source: bytes,
        format_: DocumentFormat,
        operations: list[DocumentOperation],
    ) -> ProposedDocument:
        writer = self._require_writer(format_)
        self._guard.inspect_source(source, format_)
        before = writer.read_structure(source)
        proposed = writer.mutate(source, operations)
        self._guard.inspect_result(proposed.content, format_)
        after = writer.read_structure(proposed.content)
        _verify_structure(before, after, proposed)
        return proposed

    def create(
        self,
        *,
        format_: DocumentFormat,
        payload: CreationPayload,
        job_root: Path,
        template: ApprovedTemplate | None = None,
    ) -> ProposedDocument:
        if template is not None:
            self._guard.inspect_source(template.content, template.format)
        if format_ is DocumentFormat.PDF:
            if not isinstance(payload, MarkdownCreationPayload):
                raise WriterError("unsupported_operation", "PDF creation requires Markdown")
            docx = self._writers[DocumentFormat.DOCX].create(
                payload,
                template=(template.content if template is not None else None),
            )
            content = self._renderer.convert_to_pdf(
                content=docx.content,
                format_=DocumentFormat.DOCX,
                job_root=job_root,
            )
            proposed = ProposedDocument(
                content,
                docx.structural_diff,
                docx.warnings,
                docx.writer_fingerprint + "+LibreOffice/PDF+PyMuPDF/verify",
            )
        else:
            proposed = self._require_writer(format_).create(
                payload,
                template=(template.content if template is not None else None),
            )
        if template is not None:
            proposed = ProposedDocument(
                proposed.content,
                proposed.structural_diff,
                proposed.warnings,
                (
                    f"{proposed.writer_fingerprint}+template/"
                    f"{template.template_id}@{template.sha256}"
                ),
            )
        self._guard.inspect_result(proposed.content, format_)
        self._require_writer(format_).read_structure(proposed.content)
        return proposed

    def derive_hwp(
        self,
        *,
        source: bytes,
        payload: HwpDerivationPayload,
    ) -> ProposedDocument:
        writer = self._require_writer(DocumentFormat.HWPX)
        if not isinstance(writer, HwpxWriter):
            raise WriterError("writer_unavailable", "Kordoc is unavailable", retryable=True)
        proposed = writer.derive(source, payload)
        self._guard.inspect_result(proposed.content, DocumentFormat.HWPX)
        writer.read_structure(proposed.content)
        return proposed

    def hwpx_writer(self) -> HwpxWriter | None:
        writer = self._writers.get(DocumentFormat.HWPX)
        return writer if isinstance(writer, HwpxWriter) else None

    def _require_writer(self, format_: DocumentFormat) -> DocumentWriter:
        writer = self._writers.get(format_)
        if writer is None:
            raise WriterError(
                "writer_unavailable",
                f"{format_.value} writer is unavailable",
                retryable=True,
            )
        return writer


def _verify_structure(
    before: list[StructureItem],
    after: list[StructureItem],
    proposed: ProposedDocument,
) -> None:
    if not after:
        raise WriterError("parse_after_write_failed", "proposed document has no readable structure")
    targeted = {
        diff.locator.model_dump_json(exclude_none=True)
        for diff in proposed.structural_diff
        if diff.locator
    }
    before_map = {item.locator.model_dump_json(exclude_none=True): item.value for item in before}
    after_map = {item.locator.model_dump_json(exclude_none=True): item.value for item in after}
    if before_map.keys() != after_map.keys():
        raise WriterError(
            "document_structure_changed",
            "writer added or removed native structure outside the approved operations",
        )
    for locator, value in before_map.items():
        if locator not in targeted and after_map[locator] != value:
            raise WriterError(
                "non_target_content_changed",
                "writer changed non-target canonical content",
            )
