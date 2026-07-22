from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DOCUMENT_ID_PATTERN = r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


CitationField = Literal[
    "document_id",
    "revision",
    "graph_version",
    "chunk_id",
    "section_id",
]


class AgentGuide(StrictModel):
    schema_version: Literal["1.0.0"]
    required_citations: list[CitationField]
    document_content_trust: Literal["untrusted"]
    read_acl_stage: Literal["before_retrieval"]
    write_authority: Literal["application_approval_only"]
    relation_statuses: list[Literal["VERIFIED"]]
    max_evidence_chunks: int = Field(ge=1, le=10)

    @field_validator("required_citations")
    @classmethod
    def require_all_citation_fields(
        cls,
        value: list[CitationField],
    ) -> list[CitationField]:
        required = {"document_id", "revision", "graph_version", "chunk_id", "section_id"}
        if set(value) != required or len(value) != len(required):
            raise ValueError("required_citations must contain each required field exactly once")
        return value


class Editability(StrEnum):
    EDITABLE = "editable"
    READ_ONLY = "read_only"


class AccessLevel(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


class WriteAccess(StrEnum):
    NONE = "none"
    RESTRICTED = "restricted"
    ALLOWED = "allowed"


class SourceMetadata(StrictModel):
    filename: str = Field(min_length=1)
    uri: str = Field(pattern=r"^source://")
    media_type: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SourceReference(StrictModel):
    schema_version: Literal["1.0.0"]
    kind: Literal["source_reference"]
    document_id: str = Field(pattern=DOCUMENT_ID_PATTERN, max_length=96)
    source_uri: str = Field(pattern=r"^source://")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ConversionMetadata(StrictModel):
    converter: str
    conversion_version: str
    converted_at: datetime | None = None
    status: str


class ManifestEntry(StrictModel):
    schema_version: Literal["1.0.0"]
    id: str = Field(pattern=DOCUMENT_ID_PATTERN, max_length=96)
    file_version_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
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
    ]
    language: str = Field(min_length=2)
    revision: str = Field(min_length=1)
    status: Literal["active", "inactive", "draft", "archived", "superseded"]
    official_number: str | None = None
    authority_level: str = Field(min_length=1)
    issuing_org: str | None = None
    issued_on: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    source: SourceMetadata
    canonical_path: str = Field(pattern=r"^docs/")
    access: AccessLevel
    write_access: WriteAccess
    editability: Editability
    tags: list[str]
    aliases: list[str]
    conversion: ConversionMetadata

    @model_validator(mode="after")
    def validate_effective_range(self) -> "ManifestEntry":
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to < self.effective_from
        ):
            raise ValueError("effective_to must not precede effective_from")
        return self


class Chunk(StrictModel):
    schema_version: Literal["1.0.0"]
    chunk_id: str = Field(min_length=1)
    document_id: str = Field(pattern=DOCUMENT_ID_PATTERN, max_length=96)
    file_version_id: str = Field(min_length=1)
    section_id: str = Field(pattern=r"^[0-9A-Za-z][0-9A-Za-z._:-]{0,127}$")
    section: str = Field(min_length=1)
    heading_path: list[str] = Field(min_length=1)
    text: str = Field(min_length=1)
    embedding_text: str = Field(min_length=1)
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ordinal: int = Field(ge=0)


class RelationStatus(StrEnum):
    VERIFIED = "VERIFIED"
    PROPOSED = "PROPOSED"


class Link(StrictModel):
    schema_version: Literal["1.0.0"]
    source_id: str = Field(pattern=DOCUMENT_ID_PATTERN, max_length=96)
    target_id: str = Field(pattern=DOCUMENT_ID_PATTERN, max_length=96)
    relation: str = Field(min_length=1)
    status: RelationStatus
    evidence_chunk_ids: list[str]
