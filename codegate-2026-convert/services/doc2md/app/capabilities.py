"""What doc2md can and cannot do, per format.

The consumer asked for parse and original-write-back to be reported separately,
because it keeps its document store read-only unless a verified writer exists.

**Every format reports ``write_back: false``, and that is not a placeholder.**
This service has no HWP, PDF, or DOCX writer — markitdown, pyhwp2md and PaddleOCR
are all one-directional. Claiming otherwise would invite the backend to enable
writes it cannot actually perform, so the honest answer is the useful one.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FormatCapability(BaseModel):
    format: str
    parse: bool = Field(..., description="Can extract text/tables to Markdown")
    ocr: bool = Field(..., description="Recovers text baked into images")
    page_mapping: bool = Field(
        ..., description="Populates sections[].source_page for this format"
    )
    write_back: bool = Field(
        ..., description="Can write edits back into the original binary format"
    )
    library: str
    notes: str | None = None


_TABLE: list[FormatCapability] = [
    FormatCapability(
        format="pdf",
        parse=True,
        ocr=True,
        page_mapping=True,
        write_back=False,
        library="markitdown (pdfminer) + PaddleOCR",
        notes="page_mapping은 스캔 PDF(페이지를 렌더링해 OCR한 경우)에만 채워진다. "
        "텍스트 레이어가 있는 PDF는 표 구조를 보존하기 위해 pdfminer 통합 추출을 쓰므로 "
        "source_page가 null이고, 대신 section의 char_start/char_end로 위치를 제공한다.",
    ),
    FormatCapability(
        format="pptx",
        parse=True,
        ocr=True,
        page_mapping=True,
        write_back=False,
        library="markitdown + python-pptx + PaddleOCR",
        notes="PowerPoint 없이 텍스트와 내장 이미지를 읽는다. source_page는 슬라이드 번호다.",
    ),
    FormatCapability(
        format="ppt",
        parse=True,
        ocr=False,
        page_mapping=False,
        write_back=False,
        library="markitdown",
        notes="구형 .ppt는 텍스트 변환만 지원하며 내장 이미지 OCR은 지원하지 않는다.",
    ),
    FormatCapability(
        format="hwp",
        parse=True,
        ocr=False,
        page_mapping=False,
        write_back=False,
        library="pyhwp2md",
        notes="HWP 본문에 페이지 개념이 없어 source_page는 항상 null이다.",
    ),
    FormatCapability(
        format="hwpx",
        parse=True,
        ocr=False,
        page_mapping=False,
        write_back=False,
        library="pyhwp2md",
        notes="HWPX 본문에 페이지 개념이 없어 source_page는 항상 null이다.",
    ),
    FormatCapability(
        format="docx",
        parse=True,
        ocr=False,
        page_mapping=False,
        write_back=False,
        library="markitdown",
    ),
    FormatCapability(
        format="xlsx",
        parse=True,
        ocr=False,
        page_mapping=False,
        write_back=False,
        library="markitdown",
    ),
    FormatCapability(
        format="csv",
        parse=True,
        ocr=False,
        page_mapping=False,
        write_back=False,
        library="markitdown",
    ),
    FormatCapability(
        format="html",
        parse=True,
        ocr=False,
        page_mapping=False,
        write_back=False,
        library="markitdown",
    ),
    FormatCapability(
        format="md",
        parse=True,
        ocr=False,
        page_mapping=False,
        write_back=False,
        library="markitdown",
    ),
    FormatCapability(
        format="txt",
        parse=True,
        ocr=False,
        page_mapping=False,
        write_back=False,
        library="markitdown",
    ),
]

BY_FORMAT: dict[str, FormatCapability] = {c.format: c for c in _TABLE}


class CapabilitiesResponse(BaseModel):
    service: str = "doc2md"
    version: str
    ocr_device: str
    ocr_available: bool
    formats: list[FormatCapability]
    write_back_supported: bool = Field(
        False,
        description="No verified writer exists for any binary format; keep "
        "document stores read-only with respect to this service",
    )


def table() -> list[FormatCapability]:
    return list(_TABLE)


def for_format(fmt: str) -> FormatCapability | None:
    return BY_FORMAT.get(fmt.lower().lstrip("."))
