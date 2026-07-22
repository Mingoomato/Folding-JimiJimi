from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from codegate_api.knowledge.schemas import DOCUMENT_ID_PATTERN
from codegate_api.models import ApiError, ChangePlanView, DocumentResult, ResponseType

SHA256_PATTERN = r"^[0-9a-f]{64}$"


class StrictDocumentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentFormat(StrEnum):
    HWP = "hwp"
    HWPX = "hwpx"
    DOCX = "docx"
    PPTX = "pptx"
    XLSX = "xlsx"
    PDF = "pdf"


class PlanKind(StrEnum):
    MUTATION = "mutation"
    CREATION = "creation"
    DERIVATION = "derivation"


class DocumentPlanStatus(StrEnum):
    PREPARING = "preparing"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CONSUMED = "consumed"
    FAILED = "failed"


class DocumentExecutionStatus(StrEnum):
    PREPARED = "prepared"
    FILE_APPLIED = "file_applied"
    SYNCING = "syncing"
    COMPLETED = "completed"
    SYNC_FAILED = "sync_failed"
    CONFLICT = "conflict"
    FAILED = "failed"
    UNDONE = "undone"


class NativeLocator(StrictDocumentModel):
    kind: Literal[
        "paragraph",
        "table_cell",
        "slide_paragraph",
        "slide_table_cell",
        "spreadsheet_cell",
        "pdf_page",
        "pdf_form_field",
    ]
    block_index: int | None = Field(default=None, ge=0)
    table_index: int | None = Field(default=None, ge=0)
    row_index: int | None = Field(default=None, ge=0)
    column_index: int | None = Field(default=None, ge=0)
    slide_index: int | None = Field(default=None, ge=0)
    shape_index: int | None = Field(default=None, ge=0)
    paragraph_index: int | None = Field(default=None, ge=0)
    sheet_name: str | None = Field(default=None, min_length=1, max_length=31)
    address: str | None = Field(default=None, pattern=r"^[A-Z]{1,3}[1-9][0-9]{0,6}$")
    page_index: int | None = Field(default=None, ge=0)
    field_name: str | None = Field(default=None, min_length=1, max_length=256)


class TextReplaceOperation(StrictDocumentModel):
    type: Literal["text.replace/v1"] = "text.replace/v1"
    locator: NativeLocator
    expected: str = Field(max_length=32_000)
    replacement: str = Field(max_length=32_000)


class TableCellSetOperation(StrictDocumentModel):
    type: Literal["table.cell_set/v1"] = "table.cell_set/v1"
    locator: NativeLocator
    expected: str = Field(max_length=32_000)
    replacement: str = Field(max_length=32_000)


class NullValue(StrictDocumentModel):
    type: Literal["null"] = "null"
    value: None = None


class StringValue(StrictDocumentModel):
    type: Literal["string"] = "string"
    value: str = Field(max_length=32_000)


class NumberValue(StrictDocumentModel):
    type: Literal["number"] = "number"
    value: float


class BooleanValue(StrictDocumentModel):
    type: Literal["boolean"] = "boolean"
    value: bool


class FormulaValue(StrictDocumentModel):
    type: Literal["formula"] = "formula"
    value: str = Field(min_length=2, max_length=8_192, pattern=r"^=")


CellValue = Annotated[
    NullValue | StringValue | NumberValue | BooleanValue | FormulaValue,
    Field(discriminator="type"),
]


class SpreadsheetCellSet(StrictDocumentModel):
    sheet_name: str = Field(min_length=1, max_length=31)
    address: str = Field(pattern=r"^[A-Z]{1,3}[1-9][0-9]{0,6}$")
    expected: CellValue
    replacement: CellValue


class SpreadsheetCellsSetOperation(StrictDocumentModel):
    type: Literal["spreadsheet.cells_set/v1"] = "spreadsheet.cells_set/v1"
    cells: list[SpreadsheetCellSet] = Field(min_length=1, max_length=20)


class PdfAnnotationAddOperation(StrictDocumentModel):
    type: Literal["pdf.annotation_add/v1"] = "pdf.annotation_add/v1"
    locator: NativeLocator
    text: str = Field(min_length=1, max_length=8_000)
    rect: tuple[float, float, float, float]


class PdfFormFieldSetOperation(StrictDocumentModel):
    type: Literal["pdf.form_field_set/v1"] = "pdf.form_field_set/v1"
    locator: NativeLocator
    expected: str = Field(max_length=8_000)
    replacement: str = Field(max_length=8_000)


class PdfRedactTextOperation(StrictDocumentModel):
    type: Literal["pdf.redact_text/v1"] = "pdf.redact_text/v1"
    locator: NativeLocator
    expected: str = Field(min_length=1, max_length=8_000)
    replacement: Literal[""] = ""


DocumentOperation = Annotated[
    TextReplaceOperation
    | TableCellSetOperation
    | SpreadsheetCellsSetOperation
    | PdfAnnotationAddOperation
    | PdfFormFieldSetOperation
    | PdfRedactTextOperation,
    Field(discriminator="type"),
]


class MutationPlanRequest(StrictDocumentModel):
    capability_id: str = Field(min_length=1, max_length=128)
    expected_source_sha256: str = Field(pattern=SHA256_PATTERN)
    capability_snapshot_id: str = Field(pattern=SHA256_PATTERN)
    graph_version: str = Field(min_length=1, max_length=128)
    operations: list[DocumentOperation] = Field(min_length=1, max_length=20)


class MarkdownCreationPayload(StrictDocumentModel):
    type: Literal["document.create_from_markdown/v1"] = "document.create_from_markdown/v1"
    markdown: str = Field(min_length=1, max_length=1_048_576)
    template_id: str | None = Field(default=None, max_length=128)


class WorkbookCell(StrictDocumentModel):
    address: str = Field(pattern=r"^[A-Z]{1,3}[1-9][0-9]{0,6}$")
    value: CellValue


class WorkbookSheet(StrictDocumentModel):
    name: str = Field(min_length=1, max_length=31)
    cells: list[WorkbookCell] = Field(default_factory=list, max_length=50_000)


class WorkbookCreationPayload(StrictDocumentModel):
    type: Literal["workbook.create/v1"] = "workbook.create/v1"
    sheets: list[WorkbookSheet] = Field(min_length=1, max_length=100)


class HwpDerivationPayload(StrictDocumentModel):
    type: Literal["hwp.derive_hwpx/v1"] = "hwp.derive_hwpx/v1"
    source_document_id: str = Field(pattern=DOCUMENT_ID_PATTERN)
    expected_source_sha256: str = Field(pattern=SHA256_PATTERN)


CreationPayload = Annotated[
    MarkdownCreationPayload | WorkbookCreationPayload | HwpDerivationPayload,
    Field(discriminator="type"),
]


class DocumentCreationPlanRequest(StrictDocumentModel):
    document_id: str = Field(pattern=DOCUMENT_ID_PATTERN)
    format: DocumentFormat
    capability_id: str = Field(min_length=1, max_length=128)
    capability_snapshot_id: str = Field(pattern=SHA256_PATTERN)
    graph_version: str = Field(min_length=1, max_length=128)
    target_relative_path: str = Field(
        min_length=1,
        max_length=512,
    )
    payload: CreationPayload

    @field_validator("target_relative_path")
    @classmethod
    def validate_relative_target(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        parts = normalized.split("/")
        if (
            normalized.startswith("/")
            or (len(normalized) >= 2 and normalized[0].isalpha() and normalized[1] == ":")
            or any(part in {"", ".", ".."} for part in parts)
            or "\x00" in value
        ):
            raise ValueError("target_relative_path must be a confined relative path")
        return normalized

    @model_validator(mode="after")
    def validate_payload_format(self) -> DocumentCreationPlanRequest:
        if (
            isinstance(self.payload, WorkbookCreationPayload)
            and self.format is not DocumentFormat.XLSX
        ):
            raise ValueError("workbook.create/v1 requires xlsx format")
        if (
            isinstance(self.payload, HwpDerivationPayload)
            and self.format is not DocumentFormat.HWPX
        ):
            raise ValueError("hwp.derive_hwpx/v1 requires hwpx format")
        suffix = "." + self.format.value
        if not self.target_relative_path.lower().endswith(suffix):
            raise ValueError(f"target_relative_path must end with {suffix}")
        return self


class CapabilityStatus(StrictDocumentModel):
    capability_id: str
    format: DocumentFormat
    operations: list[str]
    read: bool
    create: bool
    mutate: bool
    render: bool
    active: bool
    disabled_reasons: list[str]
    writer_name: str | None = None
    writer_version: str | None = None
    renderer_name: str | None = None
    renderer_version: str | None = None
    max_source_bytes: int = Field(gt=0)
    max_result_bytes: int = Field(gt=0)
    signed_policy: Literal["reject-mutation", "not-applicable"]
    macro_policy: Literal["read-only", "not-applicable"]


class CapabilityRegistryView(StrictDocumentModel):
    snapshot_id: str = Field(pattern=SHA256_PATTERN)
    generated_at: datetime
    capabilities: list[CapabilityStatus]


class StructureItem(StrictDocumentModel):
    locator: NativeLocator
    value: Any
    value_type: str


class SourceStructureView(StrictDocumentModel):
    document_id: str
    format: DocumentFormat
    source_sha256: str = Field(pattern=SHA256_PATTERN)
    capability_snapshot_id: str = Field(pattern=SHA256_PATTERN)
    graph_version: str
    items: list[StructureItem]


class StructuralDiff(StrictDocumentModel):
    operation_index: int = Field(ge=0)
    operation_type: str
    locator: NativeLocator | None = None
    before: Any = None
    after: Any = None


class ArtifactView(StrictDocumentModel):
    artifact_id: str
    kind: Literal["proposed", "preview_before", "preview_after", "intermediate"]
    sha256: str = Field(pattern=SHA256_PATTERN)
    mime_type: str
    byte_size: int = Field(ge=0)
    preview_label: str | None = None


class PreviewPair(StrictDocumentModel):
    locator_label: str
    before_artifact_id: str | None = None
    after_artifact_id: str | None = None
    summary_only: bool = False


class PreviewManifest(StrictDocumentModel):
    manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    pairs: list[PreviewPair]
    truncated_count: int = Field(default=0, ge=0)


class DocumentPlanView(StrictDocumentModel):
    change_plan_id: str
    kind: PlanKind
    document_id: str
    format: DocumentFormat
    capability_id: str
    source_uri: str | None
    target_relative_path: str | None
    status: DocumentPlanStatus
    base_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    proposed_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    plan_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    graph_version: str
    capability_snapshot_id: str = Field(pattern=SHA256_PATTERN)
    writer_fingerprint: str | None = None
    renderer_fingerprint: str | None = None
    operations: list[dict[str, Any]]
    structural_diff: list[StructuralDiff]
    artifacts: list[ArtifactView]
    preview_manifest: PreviewManifest | None = None
    warnings: list[str]
    error: dict[str, Any] | None = None
    created_at: datetime
    expires_at: datetime
    updated_at: datetime


class DocumentChatMessageResponse(StrictDocumentModel):
    conversation_id: str
    message_id: str
    response_type: ResponseType
    assistant_text: str
    documents: list[DocumentResult]
    change_plan: ChangePlanView | None = None
    document_plan: DocumentPlanView | None = None
    error: ApiError | None = None


class DocumentExecutionView(StrictDocumentModel):
    execution_id: str
    change_plan_id: str | None
    undo_of_execution_id: str | None
    document_id: str
    change_kind: Literal["create", "update", "derive", "recovery_remove"]
    format: DocumentFormat
    capability_id: str
    source_uri: str
    status: DocumentExecutionStatus
    before_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    after_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    artifact_sha256: str = Field(pattern=SHA256_PATTERN)
    graph_version_before: str
    graph_version_after: str | None
    error: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class DocumentContentChangedV2(StrictDocumentModel):
    schema_version: Literal["2.0.0"] = "2.0.0"
    event_id: str
    execution_id: str
    document_id: str = Field(pattern=DOCUMENT_ID_PATTERN)
    source_uri: str = Field(pattern=r"^source://")
    change_kind: Literal["create", "update", "derive", "recovery_remove"]
    format: DocumentFormat
    capability_id: str
    before_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    after_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    artifact_sha256: str = Field(pattern=SHA256_PATTERN)
    expected_source_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)


class ApproveDocumentPlanRequest(StrictDocumentModel):
    plan_hash: str = Field(pattern=SHA256_PATTERN)


class RejectDocumentPlanRequest(StrictDocumentModel):
    plan_hash: str = Field(pattern=SHA256_PATTERN)
    reason: str | None = Field(default=None, max_length=500)


class RejectDocumentPlanResponse(StrictDocumentModel):
    change_plan_id: str
    status: Literal[DocumentPlanStatus.REJECTED] = DocumentPlanStatus.REJECTED
