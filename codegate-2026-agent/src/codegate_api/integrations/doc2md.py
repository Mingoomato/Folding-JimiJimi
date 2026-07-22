from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from codegate_api.files.resolver import SourceUriResolver
from codegate_api.knowledge.schemas import ManifestEntry


class Doc2MdError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class Doc2MdSource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    filename: str
    uri: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Doc2MdFrontmatter(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: Literal["1.1.0"]
    id: str
    title: str
    doc_type: str
    language: str
    revision: str
    status: str
    official_number: str | None = None
    authority_level: str | None = None
    issuing_org: str | None = None
    issued_on: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    source: Doc2MdSource
    canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    converter_version: str = Field(min_length=1)
    access: str
    tags: list[str]
    aliases: list[str]


class Doc2MdConverter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Literal["doc2md"]
    version: str = Field(min_length=1)
    library: str = Field(min_length=1)
    ocr_device: str | None = None


class Doc2MdSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ordinal: int = Field(ge=1)
    anchor_hint: str = Field(pattern=r"^sec-[0-9]{3,}$")
    stable_key: str = Field(pattern=r"^h-[0-9a-f]{8}(?:-(?:[2-9]|[1-9][0-9]+))?$")
    level: int = Field(ge=1, le=6)
    heading: str = Field(min_length=1)
    heading_path: list[str] = Field(min_length=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=1)
    source_page: int | None = Field(default=None, ge=1)


class Doc2MdDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    severity: Literal["info", "warning", "error"]
    retryable: bool
    detail: str | None = None


class Doc2MdBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    est_tokens: int = Field(ge=0)
    budget_tokens: int = Field(ge=1)
    routing: Literal["processed", "excepted"]
    reason: str | None = None


class Doc2MdResponseV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markdown: str
    frontmatter: Doc2MdFrontmatter
    body: str
    format: str
    library_used: str
    warnings: list[str]
    cached: bool
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    converter: Doc2MdConverter
    sections: list[Doc2MdSection]
    diagnostics: list[Doc2MdDiagnostic]
    budget: Doc2MdBudget
    chunks: None = None


class Doc2MdErrorResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str
    message: str
    retryable: bool
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ConversionSection:
    ordinal: int
    stable_key: str
    heading_path: tuple[str, ...]
    char_start: int
    char_end: int
    source_page: int | None


@dataclass(frozen=True, slots=True)
class ConversionDiagnostic:
    code: str
    severity: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class ConversionResult:
    canonical_markdown: str
    body: str
    format: str
    converter: str
    warnings: tuple[str, ...]
    cached: bool
    source_sha256: str | None = None
    canonical_sha256: str | None = None
    converter_version: str | None = None
    sections: tuple[ConversionSection, ...] = ()
    diagnostics: tuple[ConversionDiagnostic, ...] = ()


class Doc2MdClient:
    """Strict consumer for the team's standalone doc2md v0.2 API."""

    def __init__(
        self,
        *,
        base_url: str,
        resolver: SourceUriResolver,
        timeout_seconds: float,
        source_kind: Literal["path", "bytes"] = "path",
        api_token: str | None = None,
        max_source_bytes: int = 33_554_432,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._resolver = resolver
        self._timeout = timeout_seconds
        self._source_kind = source_kind
        self._api_token = api_token
        self._max_source_bytes = max_source_bytes

    def convert(
        self,
        manifest: ManifestEntry,
        *,
        expected_source_sha256: str,
    ) -> ConversionResult:
        path = self._resolver.resolve(manifest.source.uri)
        self._validate_resolved_source(path)
        source = self._source_payload(
            path,
            manifest=manifest,
            expected_source_sha256=expected_source_sha256,
        )
        payload = {
            "source": source,
            "metadata": {
                "id": manifest.id,
                "title": manifest.title,
                "doc_type": manifest.doc_type,
                "language": manifest.language,
                "revision": manifest.revision,
                "status": manifest.status,
                "official_number": manifest.official_number,
                "authority_level": manifest.authority_level,
                "issuing_org": manifest.issuing_org,
                "issued_on": manifest.issued_on.isoformat() if manifest.issued_on else None,
                "effective_from": (
                    manifest.effective_from.isoformat() if manifest.effective_from else None
                ),
                "effective_to": (
                    manifest.effective_to.isoformat() if manifest.effective_to else None
                ),
                "uri": manifest.source.uri,
                "access": manifest.access.value,
                "tags": manifest.tags,
                "aliases": manifest.aliases,
            },
            "chunking": {"enabled": False},
            "expected_source_sha256": expected_source_sha256,
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_token is not None:
            headers["Authorization"] = f"Bearer {self._api_token}"
        request = Request(
            f"{self._base_url}/v2/convert",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                body = response.read()
        except HTTPError as error:
            raise self._http_error(error) from error
        except (OSError, TimeoutError, URLError) as error:
            raise Doc2MdError(
                "DOC2MD_UNAVAILABLE",
                "doc2md 서비스에 연결할 수 없습니다.",
                retryable=True,
            ) from error
        try:
            converted = Doc2MdResponseV2.model_validate_json(body)
        except ValidationError as error:
            raise Doc2MdError(
                "DOC2MD_RESPONSE_INVALID", "doc2md 응답 schema가 유효하지 않습니다."
            ) from error
        self._validate_contract(
            converted,
            manifest=manifest,
            expected_source_sha256=expected_source_sha256,
        )
        return ConversionResult(
            canonical_markdown=converted.markdown,
            body=converted.body,
            format=converted.format,
            converter=f"doc2md:{converted.converter.version}:{converted.converter.library}",
            warnings=tuple(converted.warnings),
            cached=converted.cached,
            source_sha256=converted.source_sha256,
            canonical_sha256=converted.canonical_sha256,
            converter_version=converted.converter.version,
            sections=tuple(
                ConversionSection(
                    ordinal=section.ordinal,
                    stable_key=section.stable_key,
                    heading_path=tuple(section.heading_path),
                    char_start=section.char_start,
                    char_end=section.char_end,
                    source_page=section.source_page,
                )
                for section in converted.sections
            ),
            diagnostics=tuple(
                ConversionDiagnostic(
                    code=diagnostic.code,
                    severity=diagnostic.severity,
                    retryable=diagnostic.retryable,
                )
                for diagnostic in converted.diagnostics
            ),
        )

    def health(self) -> bool:
        request = Request(f"{self._base_url}/health", headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=min(self._timeout, 5.0)) as response:  # noqa: S310
                payload: Any = json.loads(response.read())
        except (HTTPError, OSError, TimeoutError, URLError, json.JSONDecodeError):
            return False
        return isinstance(payload, dict) and payload.get("status") == "ok"

    def _validate_resolved_source(self, path: Path) -> None:
        if not path.is_file() or not path.is_relative_to(self._resolver.source_root):
            raise Doc2MdError(
                "DOC2MD_SOURCE_UNSAFE", "doc2md에 전달할 원본 경로가 안전하지 않습니다."
            )

    def _source_payload(
        self,
        path: Path,
        *,
        manifest: ManifestEntry,
        expected_source_sha256: str,
    ) -> dict[str, str]:
        if self._source_kind == "path":
            return {"kind": "path", "path": str(path)}
        try:
            with path.open("rb") as source_file:
                content = source_file.read(self._max_source_bytes + 1)
        except OSError as error:
            raise Doc2MdError(
                "DOC2MD_SOURCE_UNAVAILABLE",
                "doc2md에 전달할 원본을 읽을 수 없습니다.",
            ) from error
        if len(content) > self._max_source_bytes:
            raise Doc2MdError(
                "DOC2MD_SOURCE_TOO_LARGE",
                "doc2md에 전달할 원본이 허용 크기를 초과했습니다.",
            )
        if hashlib.sha256(content).hexdigest() != expected_source_sha256:
            raise Doc2MdError(
                "DOC2MD_STALE_SOURCE",
                "doc2md 전송 직전 원본이 승인된 버전과 달라졌습니다.",
            )
        return {
            "kind": "bytes",
            "filename": Path(manifest.source.filename).name,
            "content_base64": base64.b64encode(content).decode("ascii"),
        }

    @staticmethod
    def _http_error(error: HTTPError) -> Doc2MdError:
        payload: Doc2MdErrorResponse | None = None
        try:
            raw = error.read(65_537)
            if len(raw) <= 65_536:
                payload = Doc2MdErrorResponse.model_validate_json(raw)
        except (OSError, ValidationError):
            payload = None
        remote_code = payload.code if payload is not None else None
        code_map = {
            "SOURCE_NOT_FOUND": "DOC2MD_SOURCE_NOT_FOUND",
            "SOURCE_UNSAFE": "DOC2MD_SOURCE_UNSAFE",
            "SOURCE_HASH_MISMATCH": "DOC2MD_STALE_SOURCE",
            "UNAUTHORIZED": "DOC2MD_UNAUTHORIZED",
            "UNSUPPORTED_SOURCE": "DOC2MD_SOURCE_UNSUPPORTED",
            "CONVERSION_FAILED": "DOC2MD_CONVERSION_FAILED",
            "OCR_ENGINE_UNAVAILABLE": "DOC2MD_OCR_UNAVAILABLE",
            "CONVERSION_TIMEOUT": "DOC2MD_TIMEOUT",
            "INTERNAL_ERROR": "DOC2MD_INTERNAL_ERROR",
        }
        code = (
            code_map.get(remote_code, "DOC2MD_CONVERSION_FAILED")
            if remote_code is not None
            else "DOC2MD_CONVERSION_FAILED"
        )
        retryable = payload.retryable if payload is not None else error.code >= 500
        return Doc2MdError(
            code,
            f"doc2md가 HTTP {error.code} 오류를 반환했습니다.",
            retryable=retryable,
        )

    @staticmethod
    def _validate_contract(
        converted: Doc2MdResponseV2,
        *,
        manifest: ManifestEntry,
        expected_source_sha256: str,
    ) -> None:
        frontmatter = converted.frontmatter
        if frontmatter.id != manifest.id:
            raise Doc2MdError(
                "DOC2MD_ID_MISMATCH", "doc2md가 stable document_id를 보존하지 않았습니다."
            )
        if frontmatter.source.uri != manifest.source.uri:
            raise Doc2MdError(
                "DOC2MD_URI_MISMATCH", "doc2md가 source URI override를 보존하지 않았습니다."
            )
        if frontmatter.source.filename != Path(manifest.source.filename).name:
            raise Doc2MdError(
                "DOC2MD_METADATA_MISMATCH",
                "doc2md가 source filename을 보존하지 않았습니다.",
            )
        if frontmatter.source.sha256 != expected_source_sha256:
            raise Doc2MdError(
                "DOC2MD_STALE_CACHE",
                "doc2md 응답 source hash가 현재 원본과 달라 캐시 결과를 거부했습니다.",
            )
        if converted.source_sha256 != expected_source_sha256:
            raise Doc2MdError(
                "DOC2MD_STALE_SOURCE",
                "doc2md 응답 source hash가 현재 원본과 다릅니다.",
            )
        if (
            frontmatter.title != manifest.title
            or frontmatter.doc_type != manifest.doc_type
            or frontmatter.language != manifest.language
            or frontmatter.access != manifest.access.value
            or frontmatter.official_number != manifest.official_number
            or frontmatter.issuing_org != manifest.issuing_org
            or frontmatter.issued_on
            != (manifest.issued_on.isoformat() if manifest.issued_on else None)
            or frontmatter.effective_from
            != (manifest.effective_from.isoformat() if manifest.effective_from else None)
            or frontmatter.effective_to
            != (manifest.effective_to.isoformat() if manifest.effective_to else None)
            or frontmatter.tags != manifest.tags
            or frontmatter.aliases != manifest.aliases
        ):
            raise Doc2MdError(
                "DOC2MD_METADATA_MISMATCH",
                "doc2md가 필수 metadata override를 보존하지 않았습니다.",
            )
        if (
            frontmatter.revision != manifest.revision
            or frontmatter.status != manifest.status
            or frontmatter.authority_level != manifest.authority_level
        ):
            raise Doc2MdError(
                "DOC2MD_METADATA_MISMATCH",
                "doc2md가 revision 또는 권위 metadata override를 보존하지 않았습니다.",
            )
        if not converted.body.strip():
            raise Doc2MdError("DOC2MD_EMPTY_OUTPUT", "doc2md 변환 본문이 비어 있습니다.")
        canonical_sha256 = hashlib.sha256(
            converted.body.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        ).hexdigest()
        if (
            converted.canonical_sha256 != canonical_sha256
            or frontmatter.canonical_sha256 != canonical_sha256
        ):
            raise Doc2MdError(
                "DOC2MD_CANONICAL_HASH_MISMATCH",
                "doc2md canonical hash가 변환 본문과 일치하지 않습니다.",
            )
        if frontmatter.converter_version != converted.converter.version:
            raise Doc2MdError(
                "DOC2MD_CONVERTER_MISMATCH",
                "doc2md converter version metadata가 일치하지 않습니다.",
            )
        if converted.body not in converted.markdown:
            raise Doc2MdError(
                "DOC2MD_RESPONSE_INVALID",
                "doc2md 본문과 canonical Markdown이 일치하지 않습니다.",
            )
        Doc2MdClient._validate_sections(converted)

    @staticmethod
    def _validate_sections(converted: Doc2MdResponseV2) -> None:
        if not converted.sections:
            raise Doc2MdError(
                "DOC2MD_SECTION_MAPPING_INVALID",
                "doc2md section mapping이 비어 있습니다.",
            )
        if [section.ordinal for section in converted.sections] != list(
            range(1, len(converted.sections) + 1)
        ):
            raise Doc2MdError(
                "DOC2MD_SECTION_MAPPING_INVALID",
                "doc2md section ordinal이 연속적이지 않습니다.",
            )
        stable_keys = [section.stable_key for section in converted.sections]
        anchor_hints = [section.anchor_hint for section in converted.sections]
        if len(stable_keys) != len(set(stable_keys)) or len(anchor_hints) != len(set(anchor_hints)):
            raise Doc2MdError(
                "DOC2MD_SECTION_MAPPING_INVALID",
                "doc2md section identity가 중복됐습니다.",
            )
        if (
            converted.sections[0].level != 1
            or sum(section.level == 1 for section in converted.sections) != 1
            or any(section.level not in {1, 2} for section in converted.sections)
            or not any(section.level == 2 for section in converted.sections)
        ):
            raise Doc2MdError(
                "DOC2MD_SECTION_MAPPING_INVALID",
                "doc2md heading hierarchy가 H1/H2 계약을 충족하지 않습니다.",
            )
        previous_end = 0
        for section in converted.sections:
            if (
                section.char_start < previous_end
                or section.char_end <= section.char_start
                or section.char_end > len(converted.body)
                or section.heading_path[-1] != section.heading
            ):
                raise Doc2MdError(
                    "DOC2MD_SECTION_MAPPING_INVALID",
                    "doc2md section span 또는 heading path가 유효하지 않습니다.",
                )
            section_text = converted.body[section.char_start : section.char_end]
            if section.heading not in section_text:
                raise Doc2MdError(
                    "DOC2MD_SECTION_MAPPING_INVALID",
                    "doc2md section span이 선언한 heading을 포함하지 않습니다.",
                )
            previous_end = section.char_end
