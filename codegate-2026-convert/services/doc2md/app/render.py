"""Render or split PDF pages without invoking external desktop applications.

PPTX text is handled by markitdown and its embedded pictures are read directly
by ``app.images``; rendering a whole deck would duplicate extracted text.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

logger = logging.getLogger("doc2md.render")

# Rendering resolution. Slide text at 150 DPI lands well above the ~20px glyph
# height OCR needs, without exploding pixel count.
PDF_DPI = 150


def render_pdf_pages(path: Path) -> list[bytes]:
    """One PNG per PDF page."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.warning("PyMuPDF not installed — cannot render PDF pages")
        return []

    pages: list[bytes] = []
    try:
        with fitz.open(str(path)) as doc:
            zoom = PDF_DPI / 72
            matrix = fitz.Matrix(zoom, zoom)
            for page in doc:
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                pages.append(pix.tobytes("png"))
    except Exception as e:
        logger.warning("PDF render failed: %s", e)
        return []
    return pages


# Deliberately NOT used for text: PyMuPDF's get_text(). It is ~47x faster than
# markitdown's pdfminer/pdfplumber backend (1.4s vs 66.3s across the PDF corpus)
# and recovers exactly the same Hangul, but it returns plain text — on one
# 28K-char file it produced 0 table rows where markitdown produced 210. Tables
# are what this pipeline works hardest to preserve, so the speed is not worth it.
# PyMuPDF is used below only to *slice* the PDF; markitdown still does the
# reading.


def extract_pdf_pages_markdown(path: Path) -> list[str] | None:
    """markitdown's own output, one entry per page.

    markitdown flattens a PDF to a single string: its prose path calls
    ``pdfminer.extract_text`` on the whole file, and its form path joins
    per-page chunks with a blank line. Neither leaves a usable page boundary —
    form-feed separators survive only on the prose path.

    So we hand markitdown one page at a time, as a real single-page PDF built in
    memory. The conversion is byte-for-byte the same machinery, so table
    reconstruction is untouched: on a 5-page file both routes produce 27,927
    characters and 3,021 table pipes.

    Costs about 2.25x on a 353-page document (49s -> 111s) and returns slightly
    *more* text, because a page converted alone cannot have its content merged
    into a neighbour's block.

    Returns None when PyMuPDF is unavailable or the file cannot be sliced, so
    the caller can fall back to whole-document conversion.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return None

    from markitdown import MarkItDown, StreamInfo

    converter = MarkItDown(enable_builtins=True)
    pages: list[str] = []
    try:
        with fitz.open(str(path)) as doc:
            for i in range(len(doc)):
                single = fitz.open()
                try:
                    single.insert_pdf(doc, from_page=i, to_page=i)
                    buf = io.BytesIO(single.tobytes())
                finally:
                    single.close()
                buf.seek(0)
                try:
                    text = converter.convert_stream(
                        buf, stream_info=StreamInfo(extension=".pdf")
                    ).markdown
                except Exception as e:
                    # One unreadable page must not lose the other 352.
                    logger.warning("page %d of %s failed: %s", i + 1, path.name, e)
                    text = ""
                pages.append(text.strip())
    except Exception as e:
        logger.warning("PDF page split failed for %s: %s", path.name, e)
        return None
    return pages
