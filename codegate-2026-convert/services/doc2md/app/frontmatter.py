import hashlib
import json
import re
from pathlib import Path
from threading import Lock

import yaml

from app import __version__
from app.data_paths import atomic_write_text, state_path
from app.schemas import Frontmatter, MetadataOverrides, SourceInfo

_LOCK = Lock()

# doc_type -> id prefix. Unknown doc_types fall back to their first 3 letters, uppercased.
DOC_TYPE_PREFIX = {
    "regulation": "REG",
    "policy": "POL",
    "manual": "MAN",
    "report": "RPT",
    "form": "FRM",
    "document": "DOC",
}

_HANGUL_RE = re.compile(r"[가-힣]")
_LATIN_RE = re.compile(r"[A-Za-z]")

# markdown/HTML scaffolding whose Latin chars are not document language:
# slide markers, image/link syntax, URLs, code spans.
_LANG_NOISE = re.compile(
    r"<!--.*?-->|!?\[[^\]]*\]\([^)]*\)|https?://\S+|`[^`]*`",
    re.DOTALL,
)


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, data: dict) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def _next_id(doc_type: str) -> str:
    prefix = DOC_TYPE_PREFIX.get(doc_type, (doc_type[:3] or "DOC").upper())
    with _LOCK:
        counter_file = state_path("id_counters.json")
        counters = _load_json(counter_file)
        n = counters.get(prefix, 0) + 1
        counters[prefix] = n
        _save_json(counter_file, counters)
    return f"{prefix}-{n:06d}"


def _stable_id_for_sha(sha256: str, doc_type: str) -> str:
    """Same source content always gets the same id, even across separate /convert calls."""
    with _LOCK:
        id_file = state_path("id_by_sha256.json")
        by_sha = _load_json(id_file)
        if sha256 in by_sha:
            return by_sha[sha256]
    new_id = _next_id(doc_type)
    with _LOCK:
        id_file = state_path("id_by_sha256.json")
        by_sha = _load_json(id_file)
        by_sha[sha256] = new_id
        _save_json(id_file, by_sha)
    return new_id


_SLUG_STRIP = re.compile(r"[^0-9A-Za-z가-힣]+")


def slugify(name: str) -> str:
    """Filename -> identifier-safe slug, keeping Hangul readable.

    ``3. 입찰안내서(송파하남선 1공구).hwpx`` -> ``3_입찰안내서_송파하남선_1공구_hwpx``.
    Spaces, dots and brackets are hostile in graph/DB keys and URLs; Hangul is
    not, so it is kept rather than transliterated. The extension is part of the
    slug on purpose: a corpus routinely holds ``test (1).pdf`` next to
    ``test (1).hwp``, and dropping it would collide their identifiers.
    """
    return _SLUG_STRIP.sub("_", name).strip("_") or "doc"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_sha256(body: str) -> str:
    """Hash of the canonical body — the converted content, not the source file.

    Source and canonical are two different versioned artifacts: the same .hwpx
    can produce a different body when the converter improves, and an edit to the
    canonical Markdown does not change the source. The consumer tracks both, so
    it can tell "the source changed" apart from "our conversion changed".

    Deliberately excludes the frontmatter (which would otherwise have to contain
    its own hash) and normalises line endings, so the value does not depend on
    which platform ran the conversion.
    """
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def detect_language(text: str) -> str:
    # Strip markdown/HTML scaffolding first so slide markers and image links
    # don't skew a Korean document toward "en" on its Latin noise.
    cleaned = _LANG_NOISE.sub(" ", text)
    sample = cleaned[:4000]
    hangul = len(_HANGUL_RE.findall(sample))
    latin = len(_LATIN_RE.findall(sample))
    if hangul == 0 and latin == 0:
        return "und"
    return "ko" if hangul >= latin else "en"


def guess_title(markdown: str, fallback: str) -> str:
    for line in markdown.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip() or fallback
    return fallback


# Slide decks title badly from their content: slide 1's "title" placeholder is
# routinely a section label or template leftover ("Value", "Overview") rather
# than the deck's name, while the filename is what actually identifies it.
_FILENAME_TITLED_EXTS = {".pptx", ".ppt"}


def build(
    path: Path,
    markdown: str,
    overrides: MetadataOverrides,
    auto: dict[str, str] | None = None,
    title_hint: str | None = None,
) -> Frontmatter:
    """Assemble frontmatter. Field precedence is:
    explicit caller override > auto-extracted value > heuristic default.
    ``auto`` carries regex-extracted bid metadata; ``title_hint`` the H1 that
    postprocessing promoted.
    """
    auto = auto or {}
    sha256 = sha256_file(path)
    doc_type = overrides.doc_type or "document"
    doc_id = overrides.id or _stable_id_for_sha(sha256, doc_type)
    if path.suffix.lower() in _FILENAME_TITLED_EXTS:
        title = overrides.title or path.stem
    else:
        title = overrides.title or title_hint or guess_title(markdown, path.stem)
    language = overrides.language or detect_language(markdown or title)

    return Frontmatter(
        id=doc_id,
        title=title,
        doc_type=doc_type,
        language=language,
        revision=str(overrides.revision) if overrides.revision is not None else "1",
        status=overrides.status or "active",
        # Unchunked output is chunk 1 of 1; same slug + zero-padded shape the
        # chunker uses so both paths produce identical-looking identifiers.
        chunk_no=overrides.chunk_no or f"{slugify(path.name)}_001",
        official_number=overrides.official_number or auto.get("official_number"),
        authority_level=overrides.authority_level,
        issuing_org=overrides.issuing_org or auto.get("issuing_org"),
        issued_on=overrides.issued_on,
        effective_from=overrides.effective_from,
        effective_to=overrides.effective_to,
        source=SourceInfo(
            filename=path.name,
            uri=overrides.uri or f"source://{path.name}",
            sha256=sha256,
        ),
        canonical_sha256=canonical_sha256(markdown),
        converter_version=__version__,
        access=overrides.access or "internal",
        tags=overrides.tags or [],
        aliases=overrides.aliases or [],
    )


def render(fm: Frontmatter, body: str) -> str:
    yaml_block = yaml.safe_dump(
        fm.model_dump(),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    return f"---\n{yaml_block}---\n\n{body}"
