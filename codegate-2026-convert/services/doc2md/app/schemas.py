from typing import Literal

from pydantic import BaseModel, Field

from app.errors import Diagnostic
from app.sourcing import SourceSpec


class SourceInfo(BaseModel):
    filename: str
    uri: str
    sha256: str


class Frontmatter(BaseModel):
    # 1.1.0 adds canonical_sha256 + converter_version. Additive only, and safe:
    # the consuming adapter reads this object with extra="ignore" and never
    # parses the rendered YAML, so older readers are unaffected.
    schema_version: str = "1.1.0"
    id: str
    title: str
    doc_type: str
    language: str
    revision: str
    status: str = "active"
    # "{청킹파일명}_{청킹번호}". doc2md emits one whole document, so it defaults to
    # chunk 1 of this file; a downstream chunker overrides it per chunk.
    chunk_no: str | None = None

    official_number: str | None = None
    authority_level: str | None = None
    issuing_org: str | None = None

    issued_on: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None

    source: SourceInfo

    # Travels with the .md file itself so a canonical body can be verified and
    # attributed without calling the API again. On a chunk these describe the
    # *parent document*, which is what identifies the version it was cut from.
    canonical_sha256: str | None = None
    converter_version: str | None = None

    access: str = "internal"
    tags: list[str] = []
    aliases: list[str] = []


class MetadataOverrides(BaseModel):
    """Fields a caller can supply explicitly; anything left None is auto-derived."""

    id: str | None = None
    title: str | None = None
    doc_type: str | None = None
    language: str | None = None
    revision: str | int | None = None
    status: str | None = None
    chunk_no: str | None = None

    official_number: str | None = None
    authority_level: str | None = None
    issuing_org: str | None = None

    issued_on: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None

    uri: str | None = None
    access: str | None = None
    tags: list[str] | None = None
    aliases: list[str] | None = None


class ChunkOptions(BaseModel):
    """How to split the document for LLM consumption. Sizes are in characters."""

    enabled: bool = False
    strategy: Literal["fixed", "ratio"] = Field(
        "fixed",
        description="'fixed' caps every chunk at max_chars; 'ratio' sizes chunks "
        "as 1/parts of this document, so each document yields ~parts chunks",
    )
    parts: int = Field(5, ge=2, le=100, description="ratio strategy: target chunk count")
    max_chars: int = Field(4000, ge=200, le=1_000_000)
    min_chars: int = Field(2400, ge=0, le=1_000_000)
    # ratio strategy guards: splitting a short document into `parts` pieces
    # produces useless fragments, and a huge one could overflow a context window
    ratio_floor_chars: int = Field(2000, ge=200, le=1_000_000)
    ratio_cap_chars: int = Field(120_000, ge=1000, le=1_000_000)
    split_level: int = Field(
        2, ge=1, le=6, description="Headings at or above this level start a new chunk"
    )

    def resolve(self, body_len: int) -> tuple[int, int]:
        """Return the (max_chars, min_chars) to actually use for this document."""
        if self.strategy != "ratio":
            return self.max_chars, self.min_chars
        target = max(
            self.ratio_floor_chars, min(self.ratio_cap_chars, body_len // self.parts + 1)
        )
        return target, int(target * 0.6)


class ConvertRequest(BaseModel):
    path: str = Field(..., description="Absolute path to the source file on disk")
    metadata: MetadataOverrides = MetadataOverrides()
    chunking: ChunkOptions = ChunkOptions()
    job_id: str | None = Field(
        None,
        description="Track progress under this id; generate one client-side to "
        "poll /jobs/{job_id} while a blocking /convert runs. Optional.",
    )


class ConvertV2Request(BaseModel):
    """``POST /v2/convert``. Same options as v1 plus a polymorphic source."""

    source: SourceSpec
    metadata: MetadataOverrides = MetadataOverrides()
    chunking: ChunkOptions = ChunkOptions()
    job_id: str | None = None
    expected_source_sha256: str | None = Field(
        None,
        description="Refuse the conversion (409 SOURCE_HASH_MISMATCH) if the file "
        "does not hash to this. Guards against converting a source that changed "
        "between the caller reading it and this request arriving.",
        pattern=r"^[0-9a-f]{64}$",
    )
    budget_tokens: int | None = Field(
        None,
        description="Override the token budget for this request; defaults to "
        "DOC2MD_TOKEN_BUDGET",
    )


class RawConversion(BaseModel):
    """The expensive part: just the extracted body text, cacheable by file content."""

    body: str
    format: str
    library_used: str
    diagnostics: list[Diagnostic] = []

    @property
    def warnings(self) -> list[str]:
        """v0.1.0 representation, for the frozen ``/convert`` response."""
        return [d.as_legacy_string() for d in self.diagnostics if d.severity != "info"]


class BatchConvertRequest(BaseModel):
    """Convert several files, each tracked with its own progress/ETA."""

    paths: list[str] = Field(..., min_length=1)
    metadata: MetadataOverrides = MetadataOverrides()
    chunking: ChunkOptions = ChunkOptions()
    batch_id: str | None = None


class DocumentChunk(BaseModel):
    """One LLM-sized piece of the document, usable on its own."""

    chunk_no: str = Field(..., description="{청킹파일명}_{청킹번호}")
    index: int
    markdown: str = Field(..., description="This chunk's own frontmatter + its body")
    body: str = Field(..., description="Chunk body only, without frontmatter")
    frontmatter: Frontmatter
    heading_path: list[str] = Field(
        default_factory=list, description="Section breadcrumb this chunk sits under"
    )
    chars: int


class ConverterInfo(BaseModel):
    """Which code produced this output, so a canonical body can be attributed."""

    name: str = "doc2md"
    version: str
    library: str = Field(..., description="Extraction backend, e.g. markitdown+ocr")
    ocr_device: str | None = Field(None, description="cpu | gpu | unavailable")


class SectionInfo(BaseModel):
    """One anchorable H1/H2 region of the canonical body."""

    ordinal: int = Field(..., description="1-based index over H1/H2, document order")
    anchor_hint: str = Field(
        ..., description="Anchor the consumer is expected to generate, e.g. sec-004"
    )
    stable_key: str = Field(
        ...,
        description="Content-derived identity that survives edits elsewhere in the "
        "document; use this to match sections across revisions, not `ordinal`",
    )
    level: int
    heading: str
    heading_path: list[str]
    char_start: int = Field(..., description="Offset into `body`, inclusive")
    char_end: int = Field(..., description="Offset into `body`, exclusive")
    source_page: int | None = Field(
        None, description="Source page/slide number; null when the format has none"
    )


class BudgetInfo(BaseModel):
    """Whether this document is cheap enough to feed to a model whole."""

    est_tokens: int
    budget_tokens: int
    routing: Literal["processed", "excepted"]
    reason: str | None = None


class ConvertResultV2(BaseModel):
    """``POST /v2/convert`` — and the pipeline's internal result type.

    A superset of v1. New fields belong here; ``ConvertResultV1`` is frozen by an
    external consumer and projects down from this.
    """

    # --- identical to v0.1.0 ---
    markdown: str = Field(..., description="Full document: YAML frontmatter + body")
    frontmatter: Frontmatter
    body: str = Field(..., description="Markdown body only, without frontmatter")
    format: str
    library_used: str
    warnings: list[str] = []
    cached: bool = False
    # --- v0.2 additions ---
    source_sha256: str = Field(..., description="Hash of the original file")
    canonical_sha256: str = Field(
        ..., description="Hash of `body` (LF-normalised, frontmatter excluded)"
    )
    converter: ConverterInfo
    sections: list[SectionInfo] = []
    diagnostics: list[Diagnostic] = []
    budget: BudgetInfo
    chunks: list[DocumentChunk] | None = Field(
        None, description="Present only when chunking was requested"
    )


class ConvertResultV1(BaseModel):
    """The v0.1.0 wire format for ``POST /convert``. Do not add fields.

    The consuming backend validates this with ``extra="forbid"``, so *any*
    additional key — even one serialized as null — fails its schema check and
    the whole conversion is rejected as ``DOC2MD_RESPONSE_INVALID``. Adding
    ``chunks`` here once broke that integration silently;
    ``tests/test_v1_contract.py`` mirrors the consumer's model to keep it from
    happening again. New fields belong on ``POST /v2/convert``.
    """

    markdown: str
    frontmatter: Frontmatter
    body: str
    format: str
    library_used: str
    warnings: list[str] = []
    cached: bool = False

    @classmethod
    def of(cls, result: ConvertResultV2) -> "ConvertResultV1":
        return cls(
            markdown=result.markdown,
            frontmatter=result.frontmatter,
            body=result.body,
            format=result.format,
            library_used=result.library_used,
            warnings=result.warnings,
            cached=result.cached,
        )
