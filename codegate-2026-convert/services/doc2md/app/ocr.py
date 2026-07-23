"""Korean-first OCR with document-structure parsing, via PaddleOCR PP-StructureV3.

Plain line-by-line OCR flattens a table into a bag of loose strings — row and
column relationships are destroyed. PP-StructureV3 runs layout detection and
table-structure recognition first, so a table inside an image survives as an
actual table and headings/paragraphs keep their shape.

Pipeline: layout detection -> table structure (wired/wireless) -> Korean text
recognition (``lang="korean"``). Seal / formula / chart sub-models are disabled;
they are extra inference passes our documents do not need.

Images are processed **entirely in memory** — decoded to a numpy array and handed
straight to the model. Nothing is ever written to disk or cached: only the
recognized *text/markdown* leaves this module.

The pipeline is lazy-initialized once and reused process-wide, so formats that
need no OCR never pay the model-load cost.
"""

from __future__ import annotations

import io
import importlib.util
import logging
import os
import re
import threading
from pathlib import Path

logger = logging.getLogger("doc2md.ocr")

# A slide deck is mostly decoration: icons, bullets, logos. Running a full
# layout+table pipeline on a 123x69 arrow costs as much as a real diagram and
# yields nothing, so anything below this area is skipped outright.
MIN_IMAGE_AREA = int(os.getenv("DOC2MD_OCR_MIN_AREA", "20000"))  # ~141x141

# Photos come in at up to 12MP (3000x4000). What OCR needs is legible glyph
# height (~20px+), not absolute resolution, while cost grows with pixel count —
# so cap the long side. 1200 keeps slide text comfortably readable.
MAX_IMAGE_SIDE = int(os.getenv("DOC2MD_OCR_MAX_SIDE", "1200"))

# Default PP-StructureV3 picks *server*-grade detection and the large layout
# model. On a slide deck that means ~6 heavy model passes per image, which is
# what made a 109-image deck take 20 minutes. The mobile/small variants cut that
# sharply at a small accuracy cost, and the seal/formula/chart sub-pipelines are
# dead weight for this corpus.
# NOTE: naming any model makes PaddleOCR ignore its `lang` argument, so the
# recognition model MUST be named explicitly — otherwise it silently falls back
# to the Chinese recognizer and mangles all Hangul.
_REC_MODELS = {
    "ko": "korean_PP-OCRv5_mobile_rec",
    "en": "en_PP-OCRv5_mobile_rec",
    "ja": "japan_PP-OCRv5_mobile_rec",
    "zh": "PP-OCRv5_mobile_rec",
    "latin": "latin_PP-OCRv5_mobile_rec",
}
DEFAULT_LANG = os.getenv("DOC2MD_OCR_LANG", "ko")

# Structure mode deliberately keeps PaddleOCR's default (larger) layout model:
# swapping in PP-DocLayout-S made it misclassify tables as images, which defeats
# the only reason to run this pipeline at all. Speed here comes from the mobile
# text detector and from running structure on few images, not from a weaker
# layout model.
_BASE_MODELS = {
    "text_detection_model_name": "PP-OCRv5_mobile_det",
    "use_seal_recognition": False,
    "use_formula_recognition": False,
    "use_chart_recognition": False,
}

_HANGUL = re.compile(r"[가-힣]")
_KANA = re.compile(r"[ぁ-んァ-ン]")
_HAN = re.compile(r"[一-鿿]")
_LATIN = re.compile(r"[A-Za-z]")


def detect_lang(text: str) -> str:
    """Pick a recognition language from text already extracted from the document.

    Detection is done on the *extracted* text rather than by probing the image,
    so choosing a language costs no extra inference. Scripts are unambiguous
    enough that counting characters is sufficient.
    """
    sample = (text or "")[:4000]
    counts = {
        "ko": len(_HANGUL.findall(sample)),
        "ja": len(_KANA.findall(sample)),
        "zh": len(_HAN.findall(sample)),
        "en": len(_LATIN.findall(sample)),
    }
    best = max(counts, key=counts.get)
    if counts[best] == 0:
        return DEFAULT_LANG
    # Korean and Japanese text carries Han characters too; prefer the specific
    # script when it is present at all.
    if counts["ko"] > 0 and best == "zh":
        return "ko"
    if counts["ja"] > 0 and best == "zh":
        return "ja"
    return best


# Two OCR modes, measured on rendered slides (GPU, MX450):
#   fast      0.61 s/slide, 2.3 s init  — text detection + recognition only
#   structure 10.6 s/slide,  29 s init  — adds layout + table structure models
# Structure is 17x slower and, on slide-like images, actually returned *less*
# text (1718 vs 2222 chars) because its layout pass drops regions it does not
# classify as content. Its one real advantage is reconstructing tables, so it is
# opt-in rather than the default.
FAST = "fast"
STRUCTURE = "structure"
AUTO = "auto"
DEFAULT_MODE = os.getenv("DOC2MD_OCR_MODE", AUTO)

# AUTO: run the fast pass, then pay for structure parsing only on images whose
# recognized boxes actually look tabular. A table shows up as several rows that
# each hold several boxes at repeating x positions.
#
# Thresholds are deliberately strict. A false positive costs ~10s (a needless
# structure pass) while a false negative only loses table formatting on one
# image, so erring toward "not a table" is much cheaper. Slide decks in
# particular lay text out in loose grids that fool a lax test.
# Tuned on real slides + a real table: this pair gave 0 false positives across
# 4 slide renders while still catching a 3-row table. Loosening the ratio to
# 0.75 immediately produced a false positive.
_TABLE_MIN_ROWS = 3
_TABLE_MIN_COLS = 3
# fraction of a row's boxes that must sit on shared column positions
_TABLE_ALIGN_RATIO = 0.90

_pipelines: dict[tuple[str, str], object] = {}
_lock = threading.Lock()
_failed = False
_device_used = "unknown"
_dll_directory_handles: list[object] = []


class OcrGpuUnavailableError(RuntimeError):
    """Raised when the GPU-only OCR runtime cannot initialize CUDA."""


def _configure_nvidia_dll_directories() -> None:
    """Expose pip-installed NVIDIA runtime DLLs to Paddle on Windows."""
    if os.name != "nt" or _dll_directory_handles:
        return

    for package in ("nvidia.cublas", "nvidia.cuda_nvrtc", "nvidia.cudnn"):
        spec = importlib.util.find_spec(package)
        if spec is None or not spec.submodule_search_locations:
            continue
        for location in spec.submodule_search_locations:
            bin_dir = Path(location) / "bin"
            if not bin_dir.is_dir():
                continue
            _dll_directory_handles.append(os.add_dll_directory(str(bin_dir)))
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def _pick_device() -> str:
    """Return the first usable CUDA device.

    Paddle only reaches an NVIDIA GPU through CUDA; an Intel iGPU is not usable
    from this build regardless of being present. OCR is deliberately GPU-only:
    silently moving a large scan to CPU makes document preparation look hung
    and can also hit CPU-only inference regressions.
    """
    override = os.getenv("DOC2MD_OCR_DEVICE", "gpu").strip().lower()
    if override == "gpu":
        requested_index = None
    elif re.fullmatch(r"gpu:\d+", override):
        requested_index = int(override.split(":", 1)[1])
    else:
        raise OcrGpuUnavailableError(
            "DOC2MD_OCR_DEVICE must be 'gpu' or 'gpu:N'; CPU OCR is disabled"
        )

    _configure_nvidia_dll_directories()
    try:
        import paddle
    except Exception as e:
        raise OcrGpuUnavailableError(f"CUDA-enabled Paddle is unavailable: {e}") from e

    if not paddle.device.is_compiled_with_cuda():
        raise OcrGpuUnavailableError(
            "the installed Paddle wheel has no CUDA support; install paddlepaddle-gpu"
        )

    count = paddle.device.cuda.device_count()
    if count < 1:
        raise OcrGpuUnavailableError("no CUDA-capable NVIDIA GPU was detected")
    if requested_index is not None and requested_index >= count:
        raise OcrGpuUnavailableError(
            f"DOC2MD_OCR_DEVICE requested gpu:{requested_index}, but only {count} GPU(s) exist"
        )

    indices = [requested_index] if requested_index is not None else list(range(count))
    failures = []
    for index in indices:
        dev = f"gpu:{index}"
        try:
            # Force a real allocation so a missing CUDA runtime, an unusable
            # device, or exhausted VRAM fails before the OCR model is built.
            paddle.set_device(dev)
            image = paddle.zeros([1, 1, 8, 8])
            kernel = paddle.zeros([1, 1, 3, 3])
            _ = paddle.nn.functional.conv2d(image, kernel)
            name = paddle.device.cuda.get_device_name(index)
            logger.info("OCR using GPU %s: %s", index, name)
            return dev
        except Exception as e:
            failures.append(f"{dev}: {e}")
            logger.warning("OCR GPU candidate %s is unusable: %s", dev, e)

    raise OcrGpuUnavailableError(
        "no usable CUDA GPU remained after probing: " + "; ".join(failures)
    )


def device() -> str:
    _get_pipeline(DEFAULT_LANG, FAST)
    return _device_used


def _build(lang: str, dev: str, mode: str):
    rec = _REC_MODELS.get(lang, _REC_MODELS[DEFAULT_LANG])
    if mode == STRUCTURE:
        from paddleocr import PPStructureV3

        return PPStructureV3(
            device=dev, text_recognition_model_name=rec, **_BASE_MODELS
        )
    from paddleocr import PaddleOCR

    return PaddleOCR(
        device=dev,
        text_detection_model_name="PP-OCRv5_mobile_det",
        text_recognition_model_name=rec,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
    )


def _get_pipeline(lang: str = DEFAULT_LANG, mode: str = FAST):
    """One cached pipeline per (language, mode); built on first use."""
    global _failed, _device_used
    if lang not in _REC_MODELS:
        lang = DEFAULT_LANG
    key = (lang, mode)
    if key in _pipelines or _failed:
        return _pipelines.get(key)
    with _lock:
        if key in _pipelines or _failed:
            return _pipelines.get(key)
        try:
            dev = _device_used if _device_used.startswith("gpu:") else _pick_device()
            _pipelines[key] = _build(lang, dev, mode)
            _device_used = dev
            logger.info("OCR pipeline (lang=%s, mode=%s, device=%s) ready", lang, mode, dev)
        except Exception as e:  # paddle missing, broken CUDA, model fetch fail
            _failed = True
            _device_used = "unavailable"
            logger.error("GPU OCR unavailable; CPU fallback is disabled: %s", e)
    return _pipelines.get(key)


def available() -> bool:
    return _get_pipeline() is not None


def _decode(blob: bytes):
    """Image bytes -> BGR numpy array, in memory only (never touches disk).

    Returns None for images too small to hold document text (icons/bullets), and
    downscales anything oversized: OCR cost scales with pixel count while
    accuracy plateaus, so a 12MP photo is pure waste.
    """
    import numpy as np
    from PIL import Image

    with Image.open(io.BytesIO(blob)) as im:
        w, h = im.size
        if w * h < MIN_IMAGE_AREA:
            return None
        longest = max(w, h)
        if longest > MAX_IMAGE_SIDE:
            scale = MAX_IMAGE_SIDE / longest
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        rgb = np.array(im.convert("RGB"))
    return rgb[:, :, ::-1].copy()  # RGB -> BGR (PaddleOCR convention)


def _looks_tabular(result) -> bool:
    """Do the recognized boxes form a grid? Cheap proxy for 'has a table'.

    Uses geometry only — no extra inference — so the caller can decide whether
    the far more expensive structure pass is worth running on this image.
    """
    boxes = result.get("rec_boxes") if hasattr(result, "get") else None
    if boxes is None or len(boxes) < _TABLE_MIN_ROWS * _TABLE_MIN_COLS:
        return False

    items = []
    for b in boxes:
        x1, y1, x2, y2 = (float(v) for v in b[:4])
        items.append((y1, x1, y2 - y1))
    if not items:
        return False

    heights = sorted(h for _, _, h in items)
    tol = max(4.0, heights[len(heights) // 2] * 0.6)

    rows: list[list[float]] = []
    row_tops: list[float] = []
    for y1, x1, _ in sorted(items):
        for i, top in enumerate(row_tops):
            if abs(y1 - top) <= tol:
                rows[i].append(x1)
                break
        else:
            row_tops.append(y1)
            rows.append([x1])

    wide_rows = [r for r in rows if len(r) >= _TABLE_MIN_COLS]
    if len(wide_rows) < _TABLE_MIN_ROWS:
        return False

    # Columns must repeat across rows, not drift: take the widest row as the
    # column template and require most of every other row to land on it.
    template = sorted(max(wide_rows, key=len))
    aligned = 0
    for r in wide_rows:
        rs = sorted(r)
        hits = sum(1 for x in rs if any(abs(x - f) <= tol * 1.5 for f in template))
        if hits / len(rs) >= _TABLE_ALIGN_RATIO and hits >= _TABLE_MIN_COLS:
            aligned += 1
    return aligned >= _TABLE_MIN_ROWS


def _plain_text(result) -> str:
    texts = result.get("rec_texts") or []
    scores = result.get("rec_scores") or []
    out = []
    for i, t in enumerate(texts):
        s = float(scores[i]) if i < len(scores) else 1.0
        t = (t or "").strip()
        if t and s >= 0.6:
            out.append(t)
    return "\n".join(out)


def extract_markdown(
    blob: bytes, lang: str = DEFAULT_LANG, mode: str = DEFAULT_MODE
) -> str:
    """Return markdown for one image: tables as tables, text as text.

    ``lang`` selects the recognition model; pass what ``detect_lang`` inferred
    from the document's already-extracted text. ``mode`` is fast / structure /
    auto — auto runs the fast pass and escalates to structure only when the
    recognized layout looks tabular.

    Returns "" if OCR is unavailable or the image holds no recognizable content.
    Never raises — one bad image must not fail the whole document conversion.
    """
    try:
        image = _decode(blob)
    except Exception as e:
        logger.warning("could not decode one image: %s", e)
        return ""
    if image is None:  # decorative icon — nothing to read
        return ""

    if mode == STRUCTURE:
        return _run_structure(image, lang)

    pipeline = _get_pipeline(lang, FAST)
    if pipeline is None:
        return ""
    try:
        results = pipeline.predict(image)
    except Exception as e:
        logger.warning("OCR failed on one image: %s", e)
        return ""

    parts = []
    for res in results or []:
        if mode == AUTO and _looks_tabular(res):
            structured = _run_structure(image, lang)
            if structured:
                parts.append(structured)
                continue
        parts.append(_plain_text(res))
    return "\n\n".join(p for p in parts if p).strip()


def _run_structure(image, lang: str) -> str:
    pipeline = _get_pipeline(lang, STRUCTURE)
    if pipeline is None:
        return ""
    try:
        results = pipeline.predict(image)
    except Exception as e:
        logger.warning("structure OCR failed on one image: %s", e)
        return ""

    chunks: list[str] = []
    for res in results or []:
        try:
            md = res.markdown
        except Exception:
            continue
        text = md.get("markdown_texts", "") if isinstance(md, dict) else str(md)
        if text:
            chunks.append(text)
    return _to_clean_markdown("\n\n".join(chunks))


# --- PP-StructureV3 markdown -> plain markdown -------------------------------
# Its output wraps blocks in centering <div>s and emits tables as raw HTML.

_DIV = re.compile(r"</?div[^>]*>")
_TABLE_BLOCK = re.compile(
    r"(?:<html>\s*<body>\s*)?(<table\b.*?</table>)(?:\s*</body>\s*</html>)?",
    re.DOTALL | re.IGNORECASE,
)
_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_CELL = re.compile(r"<t[dh]\b([^>]*)>(.*?)</t[dh]>", re.DOTALL | re.IGNORECASE)
_SPAN = re.compile(r"(?:row|col)span\s*=\s*[\"']?([2-9]\d*)", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_BLANK_LINES = re.compile(r"\n{3,}")


def _to_clean_markdown(text: str) -> str:
    if not text.strip():
        return ""
    text = _TABLE_BLOCK.sub(lambda m: _table_to_markdown(m.group(1)), text)
    text = _DIV.sub("", text)
    return _BLANK_LINES.sub("\n\n", text).strip()


def _table_to_markdown(html: str) -> str:
    """Convert an HTML table to a markdown pipe table.

    Tables with merged cells are left as HTML: markdown pipe tables cannot
    express rowspan/colspan, so rewriting them would silently misalign data.
    """
    if _SPAN.search(html):
        return f"\n\n{html.strip()}\n\n"

    rows: list[list[str]] = []
    for row_html in _ROW.findall(html):
        cells = [_cell_text(c) for _, c in _CELL.findall(row_html)]
        if cells:
            rows.append(cells)
    if not rows:
        return f"\n\n{html.strip()}\n\n"

    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]

    header, *body = rows
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n\n" + "\n".join(lines) + "\n\n"


def _cell_text(html: str) -> str:
    text = _TAG.sub(" ", html)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    # a literal pipe would break the markdown row it lands in
    return re.sub(r"\s+", " ", text).strip().replace("|", "\\|")
