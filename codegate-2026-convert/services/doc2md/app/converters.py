"""Turn a document into Markdown.

**The text converter is the canonical body; OCR only fills in pictures.**

Rendering whole pages/slides to images and OCR'ing those was the earlier
default, and it was wrong. markitdown reads every PowerPoint text box and
pyhwp2md reads the HWPX body already, so re-reading the rendered page produced
the same text a second time and worse: on one deck 80% of the OCR lines
duplicated markitdown's output, a roster whose name/role pairs markitdown kept
together came back as loose 15-character fragments, and tables were flattened
into unassociated lists.

So each format converts its text directly, and OCR runs only on the pictures
embedded in it — the one thing text extraction genuinely cannot read. The
recognised text is spliced back where the picture sat (see app/images.py for how
each format's position is recovered).

The single exception is a **scanned PDF**: with no text layer there is nothing to
preserve, so the whole page is rendered and read as an image.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pyhwp2md
from markitdown import MarkItDown, MarkItDownException

from app import images, ocr, progress, render
from app.errors import Diagnostic, diagnostic
from app.schemas import RawConversion

HWP_EXTS = {".hwp", ".hwpx"}

# MarkItDown() is stateless per-call aside from its plugin registry, so one
# process-wide instance is safe to reuse across requests.
_markitdown = MarkItDown(enable_builtins=True)

# A PDF whose text layer yields less than this is treated as scanned: there is
# nothing to read without OCR.
_SCANNED_PDF_CHARS = 64

_WS = re.compile(r"\s+")


class ConversionFailedError(Exception):
    pass


def convert(path: Path, job=None) -> RawConversion:
    """Convert a file. ``job`` is an optional progress.Job to report into."""
    ext = path.suffix.lower()
    warnings: list[Diagnostic] = []

    if ext in HWP_EXTS:
        result = _convert_hwp(path, ext, warnings, job)
    elif ext == ".pptx":
        result = _convert_pptx(path, ext, warnings, job)
    elif ext == ".pdf":
        result = _convert_pdf(path, ext, warnings, job)
    else:
        progress.update(
            job, phase="convert", total=1, current=0, message=f"{ext} 변환 중"
        )
        result = _convert_other(path, ext, warnings)

    progress.update(job, phase="convert", total=1, current=1, message="변환 완료")
    return result


def page_marker(n: int) -> str:
    """Page delimiter written into the body.

    An HTML comment, so it renders as nothing but survives every markdown
    round-trip, and a reader (human or model) can always tell which page a
    passage came from. ``structure.py`` parses these back out to fill
    ``sections[].source_page``.
    """
    return f"<!--- page {n} --->"


def _join_pages(pages: dict[int, str] | list[str]) -> str:
    items = (
        sorted(pages.items())
        if isinstance(pages, dict)
        else list(enumerate(pages, start=1))
    )
    return "\n\n".join(
        f"{page_marker(n)}\n\n{text}" for n, text in items if text.strip()
    )


# ---------------------------------------------------------------------------
# per-format conversion
# ---------------------------------------------------------------------------


def _convert_hwp(
    path: Path, ext: str, warnings: list[Diagnostic], job=None
) -> RawConversion:
    progress.update(job, phase="convert", total=1, current=0, message="hwp 변환 중")
    try:
        body = pyhwp2md.convert(path)
    except pyhwp2md.Pyhwp2mdError as e:
        raise ConversionFailedError(f"hwp/hwpx conversion failed: {e}") from e

    if ext != ".hwpx":
        # Legacy .hwp is a binary CFB container and pyhwp2md exposes no image
        # data, so pictures in it cannot be read at all. Say so rather than
        # letting the text-only result look complete.
        warnings.append(diagnostic("hwp_images_unsupported"))
        return _finish(body, ext, "pyhwp2md", warnings)

    found = images.from_hwpx(path)
    texts = _read_images(found, warnings, body, job, ocr.detect_lang(body))
    if texts:
        body = _splice_by_anchor(body, found, texts)
        return _finish(body, ext, "pyhwp2md+ocr", warnings)
    return _finish(body, ext, "pyhwp2md", warnings)


def _convert_pptx(
    path: Path, ext: str, warnings: list[Diagnostic], job=None
) -> RawConversion:
    """markitdown reads the slides' own text; OCR only reads their pictures."""
    progress.update(job, phase="convert", total=1, current=0, message="pptx 텍스트 추출 중")
    try:
        body = _markitdown.convert(str(path)).markdown
    except MarkItDownException as e:
        raise ConversionFailedError(f"markitdown conversion failed: {e}") from e

    # The deck's own text tells us which recognition model to use, so picking a
    # language costs nothing extra.
    lang = ocr.detect_lang(body)
    found = images.from_pptx(path)
    texts = _read_images(found, warnings, body, job, lang)
    if texts:
        body = _splice_by_placeholder(body, found, texts)
        return _finish(body, ext, "markitdown+ocr", warnings)
    return _finish(body, ext, "markitdown", warnings)


def _convert_pdf(
    path: Path, ext: str, warnings: list[Diagnostic], job=None
) -> RawConversion:
    """Page by page, so the body carries page boundaries."""
    progress.update(job, phase="convert", total=1, current=0, message="pdf 페이지 변환 중")
    pages = render.extract_pdf_pages_markdown(path)

    if pages is None:
        # Could not slice the file — fall back to one flat conversion. Correct,
        # just without page markers.
        warnings.append(diagnostic("pdf_page_extract_failed"))
        try:
            body = _markitdown.convert(str(path)).markdown
        except MarkItDownException as e:
            raise ConversionFailedError(f"markitdown conversion failed: {e}") from e
    else:
        body = _join_pages(pages)

    if len(body.strip()) < _SCANNED_PDF_CHARS:
        return _convert_scanned_pdf(path, ext, warnings, job)

    found = images.from_pdf(path)
    texts = _read_images(found, warnings, body, job, ocr.detect_lang(body))
    if texts:
        body = _splice_by_page(body, found, texts)
        return _finish(body, ext, "markitdown+ocr", warnings)
    return _finish(body, ext, "markitdown", warnings)


def _convert_scanned_pdf(
    path: Path, ext: str, warnings: list[Diagnostic], job=None
) -> RawConversion:
    """No text layer at all — render every page and read it as an image.

    Nothing is lost by imaging here: there is no extracted text or table
    structure to preserve in the first place.
    """
    if not ocr.available():
        warnings.append(diagnostic("ocr_engine_unavailable"))
        return _finish("", ext, "markitdown", warnings)

    progress.update(
        job, phase="convert", total=1, current=0, message="스캔 PDF — 페이지 렌더링 중"
    )
    rendered = render.render_pdf_pages(path)
    if rendered:
        by_page: dict[int, str] = {}
        total = len(rendered)
        progress.update(
            job, phase="convert", total=total, current=0, message=f"페이지 OCR 0/{total}"
        )
        for i, blob in enumerate(rendered, start=1):
            md = ocr.extract_markdown(blob, lang=ocr.DEFAULT_LANG)
            if md:
                by_page[i] = md
            progress.update(job, current=i, message=f"페이지 OCR {i}/{total}")
        if by_page:
            return _finish(_join_pages(by_page), ext, "markitdown+ocr", warnings)

    warnings.append(diagnostic("scanned_pdf_ocr_failed"))
    return _finish("", ext, "markitdown", warnings)


def _convert_other(path: Path, ext: str, warnings: list[Diagnostic]) -> RawConversion:
    try:
        body = _markitdown.convert(str(path)).markdown
    except MarkItDownException as e:
        raise ConversionFailedError(f"markitdown conversion failed: {e}") from e
    return _finish(body, ext, "markitdown", warnings)


# ---------------------------------------------------------------------------
# OCR of embedded images, spliced back where the image sat
# ---------------------------------------------------------------------------


def _ocr_block(markdown: str) -> str:
    return f"\n#### 이미지 텍스트 (OCR)\n\n{markdown.strip()}\n"


# An OCR line this short is a fragment, not a sentence; keeping it only adds
# noise to a retrieval index.
_MIN_OCR_LINE = 2

# A line with no letter in it is a number, a tick mark or stray punctuation —
# never a quotable fact. One 2.3MB photo in a deck recognised as exactly
# [":", "208"], and because that photo was reused in ten places the meaningless
# "208" was spliced in ten times. Digits inside a real phrase are unaffected:
# "10,000천원" and "P1" both contain letters and survive.
_HAS_LETTER = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣA-Za-z一-鿿぀-ヿ]")


def _novel_lines(text: str, body_flat: str) -> str:
    """Keep only OCR lines that say something the body does not already.

    A picture very often restates what is beside it — a title baked into a
    banner, a caption repeated in a text box. Without this, reading pictures
    reintroduces exactly the duplication that made whole-page OCR wasteful: on
    one deck 80% of recognised lines already existed in the body.

    Comparison ignores whitespace, because OCR breaks a line wherever the layout
    does and the converter does not.
    """
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        flat = _WS.sub("", stripped)
        if len(flat) < _MIN_OCR_LINE or flat in body_flat:
            continue
        if not _HAS_LETTER.search(flat):
            continue
        kept.append(stripped)
    return "\n".join(kept)


def _read_images(
    found: list[images.EmbeddedImage],
    warnings: list[Diagnostic],
    body: str,
    job=None,
    lang: str = "ko",
) -> dict[int, str]:
    """OCR each embedded image once, keeping only what ``body`` lacks.

    Identical blobs — a logo repeated on every page — are recognised once. That
    cache is what makes reading a deck's hundred pictures affordable, and most
    of them are icons that ocr.extract_markdown skips on size anyway.
    """
    if not found:
        return {}
    if not ocr.available():
        warnings.append(diagnostic("ocr_engine_unavailable"))
        return {}

    body_flat = _WS.sub("", body)
    out: dict[int, str] = {}
    seen: dict[str, str] = {}
    total = len(found)
    progress.update(
        job, phase="convert", total=total, current=0, message=f"이미지 OCR 0/{total}"
    )
    for done, img in enumerate(found, start=1):
        key = hashlib.sha256(img.data).hexdigest()
        if key not in seen:
            # Fast mode, deliberately. The `auto` mode escalates to structure
            # parsing whenever a layout looks tabular, and that costs 15-24s per
            # image — 106 pictures in one deck took 230s against 19s before.
            # Structure parsing earns that on a *rendered page*, where the whole
            # document's tables are at stake; on an embedded picture it buys the
            # rarer case of a table screenshot. The tables that mattered are
            # already safe, because the text converter now produces them.
            seen[key] = ocr.extract_markdown(img.data, lang=lang, mode=ocr.FAST)
        novel = _novel_lines(seen[key], body_flat) if seen[key] else ""
        if novel:
            out[img.order] = novel
        progress.update(job, current=done, message=f"이미지 OCR {done}/{total}")
    return out


def _splice_by_placeholder(
    body: str, found: list[images.EmbeddedImage], texts: dict[int, str]
) -> str:
    """pptx: swap markitdown's ``![alt](그림90.jpg)`` for the recognised text.

    Two shapes can share a name, so references are consumed left to right rather
    than replaced globally.
    """
    for img in found:
        text = texts.get(img.order)
        if not text or not img.placeholder:
            continue
        pattern = re.compile(r"!\[[^\]]*\]\(" + re.escape(img.placeholder) + r"\)")
        m = pattern.search(body)
        if m:
            body = body[: m.start()] + _ocr_block(text) + body[m.end() :]
    return body


def _flatten(body: str) -> tuple[str, list[int]]:
    """Whitespace-stripped copy of ``body``, plus each kept char's real offset."""
    chars: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(body):
        if not ch.isspace():
            chars.append(ch)
            index.append(i)
    index.append(len(body))
    return "".join(chars), index


def _splice_by_anchor(
    body: str, found: list[images.EmbeddedImage], texts: dict[int, str]
) -> str:
    """hwpx: insert after the text that preceded the picture in the section XML.

    HWPX breaks one run of text across many elements, so the anchor's spacing
    almost never matches the converted Markdown. Both sides are compared with
    whitespace removed and the hit is mapped back to a real offset.
    """
    if not texts:
        return body
    flat, index = _flatten(body)
    additions: list[tuple[int, str]] = []
    for img in found:
        text = texts.get(img.order)
        if not text or not img.anchor:
            continue
        needle = _WS.sub("", img.anchor)
        if not needle:
            continue
        at = flat.find(needle)
        if at < 0:  # the anchor text did not survive conversion
            continue
        additions.append((index[min(at + len(needle), len(index) - 1)], text))

    # Insert from the back so earlier offsets stay valid.
    for pos, text in sorted(additions, reverse=True):
        body = body[:pos] + "\n" + _ocr_block(text) + body[pos:]
    return body


def _splice_by_page(
    body: str, found: list[images.EmbeddedImage], texts: dict[int, str]
) -> str:
    """pdf: append each page's recognised image text to that page's block."""
    by_page: dict[int, list[str]] = {}
    for img in found:
        text = texts.get(img.order)
        if text and img.page:
            by_page.setdefault(img.page, []).append(text)
    if not by_page:
        return body

    parts = re.split(r"(<!--+\s*page\s+\d+\s*-+->)", body)
    out = [parts[0]]
    for i in range(1, len(parts), 2):
        marker = parts[i]
        chunk = parts[i + 1] if i + 1 < len(parts) else ""
        blocks = by_page.get(int(re.search(r"\d+", marker).group()))
        if blocks:
            chunk = chunk.rstrip() + "\n" + _ocr_block("\n\n".join(blocks))
        out.append(marker)
        out.append(chunk)
    return "".join(out)


def _finish(
    body: str, ext: str, library_used: str, warnings: list[Diagnostic]
) -> RawConversion:
    if not body.strip():
        warnings.append(
            diagnostic("empty_output", detail="스캔/이미지 전용 파일일 수 있습니다")
        )
    return RawConversion(
        body=body,
        format=ext.lstrip("."),
        library_used=library_used,
        diagnostics=warnings,
    )
