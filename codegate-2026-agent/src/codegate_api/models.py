from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from codegate_api.knowledge.schemas import DOCUMENT_ID_PATTERN


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResponseType(StrEnum):
    LOCATION_RESULT = "location_result"
    TARGET_SELECTION = "target_selection"
    CHANGE_PREVIEW = "change_preview"
    ERROR = "error"


class ChangePlanStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CONSUMED = "consumed"


class ExecutionStatus(StrEnum):
    PREPARED = "prepared"
    FILE_APPLIED = "file_applied"
    SYNCING = "syncing"
    COMPLETED = "completed"
    CONFLICT = "conflict"
    FAILED = "failed"
    UNDONE = "undone"


class FileStatus(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    CONFLICT = "conflict"
    FAILED = "failed"


class SyncStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PUBLISHED = "published"
    FAILED = "failed"


class ReplaceExactOperation(StrictModel):
    type: Literal["replace_exact"] = Field(
        default="replace_exact",
        description="The only supported deterministic source edit operation.",
    )
    expected_text: str = Field(
        min_length=1,
        max_length=16_000,
        description="Smallest exact span copied from the current source and occurring once.",
    )
    replacement_text: str = Field(
        min_length=1,
        max_length=16_000,
        description="Exact replacement wording explicitly supplied by the user.",
    )
    expected_occurrences: Literal[1] = Field(
        default=1,
        description="Safety invariant requiring exactly one current-source match.",
    )


class ChangePlanView(StrictModel):
    change_plan_id: str
    document_id: str
    file_version_id: str
    source_uri: str
    operation: ReplaceExactOperation
    unified_diff: str
    base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposed_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: ChangePlanStatus
    created_at: datetime
    expires_at: datetime


class ExecutionView(StrictModel):
    execution_id: str
    change_plan_id: str | None
    undo_of_execution_id: str | None
    document_id: str
    status: ExecutionStatus
    file_status: FileStatus
    sync_status: SyncStatus
    before_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_version_id: str | None
    graph_version_before: str
    graph_version_after: str | None
    error: "ApiError | None" = None
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def terminal(self) -> bool:
        return self.sync_status is SyncStatus.FAILED or self.status in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.CONFLICT,
            ExecutionStatus.FAILED,
            ExecutionStatus.UNDONE,
        }

    @computed_field  # type: ignore[prop-decorator]
    @property
    def stage(self) -> str:
        if self.file_status is FileStatus.PENDING:
            return "file_write"
        if self.file_status in {FileStatus.CONFLICT, FileStatus.FAILED}:
            return "file_failed"
        if self.sync_status is SyncStatus.PENDING:
            return "sync_queued"
        if self.sync_status is SyncStatus.RUNNING:
            return "sync_running"
        if self.sync_status is SyncStatus.FAILED:
            return "sync_retryable" if self.error and self.error.retryable else "sync_failed"
        return "published"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def recommended_poll_after_ms(self) -> int | None:
        return None if self.terminal else 500


class ApproveChangePlanRequest(StrictModel):
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class RejectChangePlanRequest(StrictModel):
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str | None = Field(default=None, max_length=500)


class RejectChangePlanResponse(StrictModel):
    change_plan_id: str
    status: ChangePlanStatus


class SourceSyncRequest(StrictModel):
    base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: str = Field(max_length=1_048_576)
    operation: ReplaceExactOperation


class Evidence(StrictModel):
    chunk_id: str
    section_id: str
    section: str
    heading_path: list[str]
    quote: str


class Relation(StrictModel):
    relation: str
    target_document_id: str


class DocumentResult(StrictModel):
    document_id: str
    file_version_id: str
    revision: str
    authority_level: str
    title: str
    display_path: str
    source_uri: str
    score: float = Field(ge=0, le=1)
    graph_version: str
    evidence: list[Evidence]
    citations: list[str]
    relations: list[Relation]
    can_read: bool
    can_write: bool
    editability: str


class ApiError(StrictModel):
    code: str
    message: str
    retryable: bool = False


class ChatMessageRequest(StrictModel):
    conversation_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4_000)
    selected_document_id: str | None = Field(
        default=None,
        pattern=DOCUMENT_ID_PATTERN,
        max_length=96,
    )


class ChatMessageResponse(StrictModel):
    conversation_id: str
    message_id: str
    response_type: ResponseType
    assistant_text: str
    documents: list[DocumentResult]
    change_plan: ChangePlanView | None = None
    error: ApiError | None = None


class HealthResponse(StrictModel):
    status: str
    knowledge_version: str
    converter_available: bool
    graph_available: bool
    index_artifacts_available: bool
    worker_available: bool
    pending_sync_events: int
    failed_sync_events: int
    stale_sync_events: int
    agent_available: bool
    auth_mode: str
    persistence_available: bool


class AuthMeResponse(StrictModel):
    authenticated: Literal[True] = True
    subject_id: str
    tenant_id: str
    email: str | None = None
    read_access: list[str]
    write_scope: Literal["none", "documents", "workspace"]
    writable_document_ids: list[str]
    provisioned: bool
    authz_source: str


class DemoResetResponse(StrictModel):
    status: Literal["reset"]
    knowledge_version: str
