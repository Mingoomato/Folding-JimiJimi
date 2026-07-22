"""Find the images embedded in a document, and where they sit in its text.

Rendering a whole page/slide to an image and OCR'ing that was the wrong default.
The converters already read the document's real text — markitdown reads every
PowerPoint text box, pyhwp2md reads the HWPX body — so OCR'ing the rendered page
re-read text we already had, more badly: on one deck 80% of the OCR lines
duplicated markitdown's output, and a roster whose name/role pairs markitdown
had kept together ("Chaejeong Heo" / "CEO · CTO") came back as loose fragments.
Tables suffered the same way.

So the text conversion is the canonical body, and OCR only fills in the one
thing it alone can read: text baked inside a picture. To splice that back into
the right place, each image needs a position:

  pptx   ``placeholder`` — markitdown already writes ``![alt](그림90.jpg)`` at the
         image's spot, and the filename is derived from the shape name, so the
         reference can be reconstructed exactly and swapped in place.
  hwpx   ``anchor`` — pyhwp2md writes nothing for images, but the HWPX package is
         a zip whose section XML holds ``<hp:pic>`` in document order. The text
         just before it locates the insertion point in the converted Markdown.
  pdf    ``page`` — the body already carries ``<!--- page N --->`` markers, so an
         image only needs its page number and its vertical position on it.

Legacy binary ``.hwp`` is not covered: it is a CFB container, and pyhwp2md
exposes no image data. Those documents convert text-only and say so.
"""

from __future__ import annotations

import logging
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

logger = logging.getLogger("doc2md.images")

# Anything smaller is a bullet, icon or rule — never worth a model pass.
MIN_BYTES = 3000
# How much preceding text to remember as an insertion anchor.
ANCHOR_CHARS = 40


@dataclass
class EmbeddedImage:
    """One picture, plus where its text belongs in the converted Markdown."""

    data: bytes
    order: int
    placeholder: str | None = None  # exact ``![...](file)`` to replace (pptx)
    anchor: str | None = None  # text to insert after (hwpx)
    page: int | None = None  # page number (pdf)


# ---------------------------------------------------------------------------
# pptx
# ---------------------------------------------------------------------------


def _shape_ref(name: str) -> str:
    """Rebuild the reference markitdown writes for a picture shape.

    Mirrors markitdown's pptx converter: ``re.sub(r"\\W", "", shape.name) + ".jpg"``.
    ``\\W`` is Unicode-aware, so a Korean shape name survives intact.
    """
    return re.sub(r"\W", "", name, flags=re.UNICODE) + ".jpg"


def _iter_pptx_pictures(shapes):
    for shape in shapes:
        if getattr(shape, "shape_type", None) == 6:  # GROUP
            yield from _iter_pptx_pictures(shape.shapes)
            continue
        if getattr(shape, "shape_type", None) != 13:  # PICTURE
            continue
        # python-pptx raises ValueError (not AttributeError) when a picture's
        # embed relationship is broken, so a hasattr() check would not catch it.
        try:
            blob = shape.image.blob
        except Exception:
            continue
        yield shape, blob


def from_pptx(path: Path) -> list[EmbeddedImage]:
    try:
        from pptx import Presentation

        prs = Presentation(str(path))
    except Exception as e:
        logger.warning("pptx image scan failed: %s", e)
        return []

    out: list[EmbeddedImage] = []
    for slide in prs.slides:
        for shape, blob in _iter_pptx_pictures(slide.shapes):
            if len(blob) < MIN_BYTES:
                continue
            out.append(
                EmbeddedImage(
                    data=blob,
                    order=len(out),
                    placeholder=_shape_ref(shape.name or ""),
                )
            )
    return out


# ---------------------------------------------------------------------------
# hwpx
# ---------------------------------------------------------------------------

_HWPX_TEXT = "}t"  # local name of <hp:t>
_HWPX_PIC = "}pic"  # local name of <hp:pic>
_HWPX_IMG = "}img"  # local name of <hc:img>


def _hwpx_manifest(zf: zipfile.ZipFile) -> dict[str, str]:
    """binaryItemIDRef -> archive path, from Contents/content.hpf."""
    try:
        hpf = zf.read("Contents/content.hpf").decode("utf-8", "replace")
    except KeyError:
        return {}
    return {
        m.group(1): m.group(2)
        for m in re.finditer(r'<opf:item\s+id="([^"]+)"\s+href="([^"]+)"', hpf)
    }


def from_hwpx(path: Path) -> list[EmbeddedImage]:
    """Pictures in body order, each with the text that precedes it.

    HWPX splits a run of text across many ``<hp:t>`` elements (a title can come
    through as 찰/안/내/서), so the anchor is built by concatenating them as the
    tree is walked rather than reading any single element.
    """
    try:
        zf = zipfile.ZipFile(path)
    except Exception as e:
        logger.warning("hwpx image scan failed: %s", e)
        return []

    with zf:
        manifest = _hwpx_manifest(zf)
        sections = sorted(
            n for n in zf.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n)
        )
        out: list[EmbeddedImage] = []
        for name in sections:
            try:
                root = ElementTree.fromstring(zf.read(name))
            except ElementTree.ParseError as e:
                logger.warning("hwpx section %s unparseable: %s", name, e)
                continue

            text: list[str] = []
            for el in root.iter():
                tag = el.tag
                if tag.endswith(_HWPX_TEXT):
                    if el.text:
                        text.append(el.text)
                    continue
                if not tag.endswith(_HWPX_PIC):
                    continue
                ref = next(
                    (
                        img.get("binaryItemIDRef")
                        for img in el.iter()
                        if img.tag.endswith(_HWPX_IMG)
                    ),
                    None,
                )
                if not ref:
                    continue
                href = manifest.get(ref)
                if not href:
                    continue
                try:
                    blob = zf.read(href)
                except KeyError:
                    continue
                if len(blob) < MIN_BYTES:
                    continue
                out.append(
                    EmbeddedImage(
                        data=blob,
                        order=len(out),
                        anchor="".join(text)[-ANCHOR_CHARS:].strip() or None,
                    )
                )
        return out


# ---------------------------------------------------------------------------
# pdf
# ---------------------------------------------------------------------------


def from_pdf(path: Path) -> list[EmbeddedImage]:
    """Pictures with their page number, ordered down the page.

    Only the page is recorded, not a text anchor: the body already carries a
    ``<!--- page N --->`` marker per page, which is a far more reliable insertion
    point than matching extracted prose.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return []

    out: list[EmbeddedImage] = []
    try:
        with fitz.open(str(path)) as doc:
            for page_no, page in enumerate(doc, start=1):
                placed = []
                for info in page.get_images(full=True):
                    xref = info[0]
                    try:
                        rects = page.get_image_rects(xref)
                        top = min((r.y0 for r in rects), default=0.0)
                        blob = doc.extract_image(xref)["image"]
                    except Exception:
                        continue
                    if len(blob) < MIN_BYTES:
                        continue
                    placed.append((top, blob))
                for _, blob in sorted(placed, key=lambda p: p[0]):
                    out.append(
                        EmbeddedImage(data=blob, order=len(out), page=page_no)
                    )
    except Exception as e:
        logger.warning("pdf image scan failed: %s", e)
        return []
    return out
