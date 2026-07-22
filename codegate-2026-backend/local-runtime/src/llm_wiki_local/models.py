from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SourceEventType(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"


EVENT_TYPE_ALIASES = {
    "create": SourceEventType.CREATED,
    "created": SourceEventType.CREATED,
    "update": SourceEventType.UPDATED,
    "updated": SourceEventType.UPDATED,
    "delete": SourceEventType.DELETED,
    "deleted": SourceEventType.DELETED,
}


class ConversionMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$",
        max_length=96,
    )
    title: str | None = Field(default=None, min_length=1, max_length=500)
    doc_type: Literal[
        "regulation",
        "policy",
        "procedure",
        "manual",
        "guide",
        "specification",
        "contract",
        "report",
        "meeting_note",
        "general",
    ] = "general"
    language: str | None = Field(
        default=None,
        pattern=r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$",
    )
    revision: str | int | None = None
    status: Literal["active", "draft", "archived", "superseded"] = "active"
    official_number: str | None = None
    authority_level: Literal[
        "regulation",
        "policy",
        "contract",
        "procedure",
        "manual",
        "guide",
        "specification",
        "report",
        "reference",
    ] | None = None
    issuing_org: str | None = None
    issued_on: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    uri: str | None = None
    access: Literal["public", "internal", "restricted"] = "internal"
    tags: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)


class SourceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=128)
    event_type: SourceEventType
    source_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=0)
    relative_path: str = Field(min_length=1, max_length=2048)
    source_path: str | None = None
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    base_source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: ConversionMetadata = Field(default_factory=ConversionMetadata)

    @field_validator("event_type", mode="before")
    @classmethod
    def normalize_event_type(cls, value: Any) -> Any:
        if isinstance(value, str):
            return EVENT_TYPE_ALIASES.get(value.lower(), value)
        return value

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts or not path.name:
            raise ValueError("relative_path must be a safe relative path")
        return path.as_posix()

    @model_validator(mode="after")
    def validate_source_fields(self) -> SourceEvent:
        if (
            self.event_type in {SourceEventType.CREATED, SourceEventType.UPDATED}
            and not self.source_path
        ):
            raise ValueError("created/updated events require source_path")
        if self.event_type == SourceEventType.UPDATED and not self.base_source_sha256:
            raise ValueError("updated events require base_source_sha256")
        if self.event_type == SourceEventType.DELETED and not self.base_source_sha256:
            raise ValueError("deleted events require base_source_sha256")
        return self


class EventSubmission(BaseModel):
    accepted: bool = True
    duplicate: bool = False
    event_id: str
    job_id: str
    status: str


class BuildOutcome(BaseModel):
    build_id: str
    activated: bool


class SourceRecord(BaseModel):
    source_id: str
    doc_id: str
    relative_path: str
    status: str
    active_sha256: str | None
    source_version: int
    last_sequence: int
    metadata: dict[str, Any]
    fragment_files: list[str]
    updated_at: str
