"""Structured warnings and errors.

v0.1.0 reported problems as bare strings (``"pptx_ocr_skipped: ..."``), which
forces the caller to parse prose to decide what to do. The integration contract
asks for the one bit that actually drives behaviour: **is retrying worth it?**

Retryable means the *same input* could succeed later — the OCR engine was
missing or the GPU was busy. A document that simply
contains no text is not retryable, no matter how often it is submitted.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["info", "warning", "error"]


class Diagnostic(BaseModel):
    """One problem encountered while converting, in machine-readable form."""

    code: str
    message: str
    severity: Severity = "warning"
    retryable: bool = False
    detail: str | None = None

    def as_legacy_string(self) -> str:
        """The v0.1.0 ``warnings[]`` representation: ``"code: message"``."""
        return f"{self.code}: {self.detail or self.message}"


# code -> (severity, retryable, human message)
_CATALOG: dict[str, tuple[Severity, bool, str]] = {
    # OCR / rendering — the engine or a host dependency was unavailable, so the
    # same file may well convert better on a healthier host.
    "ocr_engine_unavailable": ("warning", True, "OCR 엔진을 사용할 수 없어 이미지 텍스트를 건너뛰었습니다"),
    "scanned_pdf_ocr_failed": ("warning", True, "스캔 PDF에서 텍스트를 복구하지 못했습니다"),
    "pptx_ocr_skipped": ("warning", True, "슬라이드 이미지 OCR을 건너뛰었습니다"),
    "slide_render_unavailable": ("warning", True, "슬라이드를 렌더링할 수 없어 내장 이미지로 대체했습니다"),
    "pdf_page_extract_failed": ("warning", True, "페이지 단위 추출에 실패해 통합 추출로 대체했습니다"),
    # Inherent to the document — retrying changes nothing.
    "empty_output": ("warning", False, "변환 결과 본문이 비어 있습니다"),
    "heading_normalized": ("info", False, "heading 구조를 계약 요건에 맞게 보정했습니다"),
    "structure_incomplete": (
        "warning",
        False,
        "제목 외에 본문이 없어 비어있지 않은 H2를 만들 수 없었습니다",
    ),
    "no_page_mapping": ("info", False, "이 형식은 페이지 정보를 제공하지 않습니다"),
    "hwp_images_unsupported": (
        "info",
        False,
        "구형 .hwp는 이미지 추출이 불가능해 본문 텍스트만 변환했습니다",
    ),
    "token_budget_exceeded": ("warning", False, "토큰 예산을 초과해 예외로 분류했습니다"),
}


def diagnostic(code: str, detail: str | None = None, **overrides) -> Diagnostic:
    """Build a Diagnostic from the catalog. Unknown codes default to a
    non-retryable warning rather than raising — a diagnostic must never be the
    thing that breaks a conversion."""
    severity, retryable, message = _CATALOG.get(
        code, ("warning", False, code.replace("_", " "))
    )
    return Diagnostic(
        code=code,
        message=overrides.get("message", message),
        severity=overrides.get("severity", severity),
        retryable=overrides.get("retryable", retryable),
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

# code -> (http status, retryable)
ERROR_CODES: dict[str, tuple[int, bool]] = {
    "SOURCE_NOT_FOUND": (404, False),
    "SOURCE_UNSAFE": (403, False),
    # The file on disk is not the one the caller meant to convert. Retrying the
    # same request cannot help — the caller must re-read the source and decide.
    "SOURCE_HASH_MISMATCH": (409, False),
    "UNAUTHORIZED": (401, False),
    "UNSUPPORTED_SOURCE": (400, False),
    "CONVERSION_FAILED": (422, False),
    "OCR_ENGINE_UNAVAILABLE": (503, True),
    "CONVERSION_TIMEOUT": (504, True),
    "INTERNAL_ERROR": (500, True),
}


class Doc2MdError(Exception):
    """Raised anywhere in the pipeline; rendered by the handler in main.py.

    The v0.1.0 consumer keys off the HTTP status alone and ignores the body, so
    adding this structure is backwards compatible as long as the status codes
    above stay put.
    """

    def __init__(self, code: str, message: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    @property
    def status_code(self) -> int:
        return ERROR_CODES.get(self.code, (500, True))[0]

    @property
    def retryable(self) -> bool:
        return ERROR_CODES.get(self.code, (500, True))[1]

    def body(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "detail": self.detail,
            # v0.1.0 clients read `detail`; FastAPI's default error body uses it
            # too, so keeping it populated avoids surprising anyone mid-migration.
        }


class ErrorResponse(BaseModel):
    """Documented shape of a 4xx/5xx body (for OpenAPI)."""

    code: str = Field(..., examples=["CONVERSION_FAILED"])
    message: str
    retryable: bool
    detail: str | None = None
