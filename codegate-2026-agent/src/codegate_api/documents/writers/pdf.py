# mypy: disable-error-code="arg-type,no-untyped-call,var-annotated"
from __future__ import annotations

import importlib.metadata
from typing import Any

import pymupdf

from codegate_api.documents.models import (
    CreationPayload,
    DocumentFormat,
    DocumentOperation,
    NativeLocator,
    PdfAnnotationAddOperation,
    PdfFormFieldSetOperation,
    PdfRedactTextOperation,
    StructuralDiff,
    StructureItem,
)
from codegate_api.documents.writers.base import ProposedDocument, WriterError


class PdfWriter:
    format = DocumentFormat.PDF

    def __init__(self) -> None:
        self.fingerprint = "PyMuPDF/" + importlib.metadata.version("PyMuPDF")

    def read_structure(self, source: bytes) -> list[StructureItem]:
        document = _load(source)
        try:
            items: list[StructureItem] = []
            for page_index, page in enumerate(document):
                items.append(
                    StructureItem(
                        locator=NativeLocator(kind="pdf_page", page_index=page_index),
                        value=page.get_text("text"),
                        value_type="text",
                    )
                )
                widgets = page.widgets() or []
                for widget in widgets:
                    if widget.field_name:
                        items.append(
                            StructureItem(
                                locator=NativeLocator(
                                    kind="pdf_form_field",
                                    page_index=page_index,
                                    field_name=widget.field_name,
                                ),
                                value=widget.field_value or "",
                                value_type="form_field",
                            )
                        )
            return items
        finally:
            document.close()

    def mutate(
        self,
        source: bytes,
        operations: list[DocumentOperation],
    ) -> ProposedDocument:
        document = _load(source)
        try:
            _reject_restricted(document)
            diffs: list[StructuralDiff] = []
            redaction_pages: set[int] = set()
            for index, operation in enumerate(operations):
                if isinstance(operation, PdfAnnotationAddOperation):
                    page = _page(document, operation.locator)
                    rect = pymupdf.Rect(operation.rect)
                    if rect.is_empty or not page.rect.contains(rect):
                        raise WriterError("invalid_locator", "PDF annotation rectangle is invalid")
                    annotation = page.add_freetext_annot(rect, operation.text)
                    annotation.update()
                    before: Any = None
                    after: Any = operation.text
                elif isinstance(operation, PdfFormFieldSetOperation):
                    page = _page(document, operation.locator)
                    widget = _widget(page, operation.locator)
                    before = widget.field_value or ""
                    if before != operation.expected:
                        raise WriterError("expected_value_mismatch", "PDF form field changed")
                    widget.field_value = operation.replacement
                    widget.update()
                    after = operation.replacement
                elif isinstance(operation, PdfRedactTextOperation):
                    page = _page(document, operation.locator)
                    matches = page.search_for(operation.expected)
                    if len(matches) != 1:
                        raise WriterError(
                            "expected_value_mismatch",
                            "PDF exact text must resolve to exactly one rectangle "
                            "on the selected page",
                        )
                    page.add_redact_annot(matches[0], fill=(0, 0, 0))
                    redaction_pages.add(operation.locator.page_index or 0)
                    before = operation.expected
                    after = ""
                else:
                    raise WriterError("unsupported_operation", "PDF operation is unsupported")
                diffs.append(
                    StructuralDiff(
                        operation_index=index,
                        operation_type=operation.type,
                        locator=operation.locator,
                        before=before,
                        after=after,
                    )
                )
            for page_index in redaction_pages:
                document[page_index].apply_redactions()
            result = document.tobytes(garbage=4, deflate=True, clean=True)
        finally:
            document.close()
        check = _load(result)
        check.close()
        return ProposedDocument(result, diffs, [], self.fingerprint)

    def create(
        self,
        payload: CreationPayload,
        *,
        template: bytes | None = None,
    ) -> ProposedDocument:
        if template is not None:
            raise WriterError("unsupported_template", "PDF templates are not supported")
        raise WriterError(
            "renderer_unavailable",
            "PDF Markdown creation requires the managed DOCX-to-LibreOffice pipeline",
            retryable=True,
        )


def _load(source: bytes) -> pymupdf.Document:
    try:
        return pymupdf.open(stream=source, filetype="pdf")
    except Exception as error:
        raise WriterError("malformed_document", "PDF could not be parsed") from error


def _reject_restricted(document: pymupdf.Document) -> None:
    if document.needs_pass:
        raise WriterError("encrypted_document", "encrypted PDF mutation is forbidden")
    if document.get_sigflags() > 0:
        raise WriterError("signed_document", "signed PDF mutation is forbidden")
    if any(
        widget.field_type_string == "Signature"
        for page_index in range(document.page_count)
        for widget in (document[page_index].widgets() or [])
    ):
        raise WriterError("signed_document", "signed PDF mutation is forbidden")


def _page(document: pymupdf.Document, locator: NativeLocator) -> pymupdf.Page:
    if locator.page_index is None or locator.kind not in {"pdf_page", "pdf_form_field"}:
        raise WriterError("invalid_locator", "PDF operation requires a page locator")
    try:
        return document[locator.page_index]
    except IndexError as error:
        raise WriterError("invalid_locator", "PDF page does not exist") from error


def _widget(page: pymupdf.Page, locator: NativeLocator) -> Any:
    if locator.kind != "pdf_form_field" or not locator.field_name:
        raise WriterError("invalid_locator", "PDF form update requires a field locator")
    matches = [
        widget for widget in (page.widgets() or []) if widget.field_name == locator.field_name
    ]
    if len(matches) != 1:
        raise WriterError("invalid_locator", "PDF form field does not resolve uniquely")
    return matches[0]
