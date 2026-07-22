"""Deterministic, safe cleanups applied to converted markdown before it is
wrapped with frontmatter.

Only transforms that cannot corrupt correct content live here:
  1. strip_image_refs     — drop dangling ![alt](file) image references
  2. collapse_glyph_runs  — undo PDF layered-glyph garble (실실실…→실)
  3. strip_empty_tables   — drop layout tables that came through fully blank
  4. promote_title        — give the doc an H1 when it lacks one, and report the title

Heading *levels* are not this module's concern: clean() hands its result to
structure.normalize_headings, which enforces the invariants the downstream wiki
builder needs (one H1, a non-empty H2, no level jumps).

Intentionally NOT done (too risky to guess, would make output worse):
  - splitting a table cell whose rows were flattened during extraction
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app import structure

# PDF layered-glyph garble repeats *every* character ~10-15x in a row
# (실실실…실시시시…시  →  실시). We undo it in three tiers, each tuned so it
# cannot touch legitimate content or markdown structure:
#   - letters (Hangul/Latin): 4+  — real words never repeat one letter 4x
#   - other punctuation (e.g. the "[" in "[BIM]"): 6+, and never -,|,= which
#     markdown rules/table separators are built from
#   - spaces: 6+  — collapse only the extreme runs the garble produces
_FENCE = re.compile(r"^\s*(```|~~~)")
_GLYPH_LETTER = re.compile(r"([가-힣ㄱ-ㅎㅏ-ㅣA-Za-z])\1{3,}")
_GLYPH_PUNCT = re.compile(r"([^\w\s\-|=])\1{5,}")
_SPACE_RUN = re.compile(r" {6,}")


def collapse_glyph_runs(text: str) -> str:
    text = _GLYPH_LETTER.sub(r"\1", text)
    text = _GLYPH_PUNCT.sub(r"\1", text)
    text = _SPACE_RUN.sub(" ", text)
    return text


# markitdown emits `![alt](original_filename.jpg)` for every picture shape, but
# we never save that image anywhere (no disk write, no base64, no cache) — the
# reference is dangling from the moment it's written. Only OCR'd text should
# represent what was in an image, so the raw reference is pure noise; drop it.
_IMAGE_REF = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")


def strip_image_refs(text: str) -> str:
    text = _IMAGE_REF.sub("", text)
    return _EXCESS_BLANK_LINES.sub("\n\n", text)


def _is_table_row(s: str) -> bool:
    s = s.strip()
    return len(s) >= 2 and s.startswith("|") and s.endswith("|")


def _is_separator_row(s: str) -> bool:
    cells = [c.strip() for c in s.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-+:?", c or "") for c in cells)


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def strip_empty_tables(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if _is_table_row(lines[i]):
            j = i
            block: list[str] = []
            while j < n and _is_table_row(lines[j]):
                block.append(lines[j])
                j += 1
            data = [
                c
                for row in block
                if not _is_separator_row(row)
                for c in _cells(row)
            ]
            if data and all(c == "" for c in data):
                # fully-empty table: drop it (and a trailing blank line if any)
                if j < n and lines[j].strip() == "":
                    j += 1
            else:
                out.extend(block)
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


MAX_LINE_CHARS = 500

# Break points in falling order of preference. A wrap should land where the
# reader would already have paused, so meaning survives the break.
_SENTENCE_END = re.compile(r"(?<=[.!?。？！])\s|(?<=다\.)\s|(?<=음\.)\s|(?<=임\.)\s")
_CLAUSE_END = re.compile(r"(?<=[,;:，、·])\s|(?<=하여)\s|(?<=하고)\s|(?<=되며)\s")
_ANY_SPACE = re.compile(r"\s")


def wrap_long_lines(text: str, limit: int = MAX_LINE_CHARS) -> str:
    """Hard-cap every line at ``limit`` characters, breaking where it reads best.

    Downstream evidence extraction works in quotes of a few hundred characters,
    so a single 700-character line either overflows that budget or gets cut at an
    arbitrary offset. Breaking it here, at a sentence boundary, means the cut
    lands where a reader would already have paused.

    The cap is unconditional, **including table rows**: a row longer than the
    limit is wrapped even though its continuation lines no longer parse as part
    of the table. That is a deliberate trade — the limit matters more than the
    row staying machine-readable — and it affects few rows (16 in the corpus).
    Rows at or under the limit are untouched, so repair_tables' work stands.

    Fenced code is the one exemption, because whitespace there carries meaning
    and a wrap would change what the code does.
    """
    out: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        m = _FENCE.match(line)
        if m:
            token = m.group(1)
            fence = None if fence and line.strip().startswith(fence) else fence or token
            out.append(line)
            continue
        if fence is not None or len(line) <= limit:
            out.append(line)
            continue
        out.extend(_wrap_one(line, limit))
    return "\n".join(out)


def _wrap_one(line: str, limit: int) -> list[str]:
    pieces: list[str] = []
    rest = line
    while len(rest) > limit:
        cut = _best_cut(rest, limit)
        pieces.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        pieces.append(rest)
    return pieces or [line]


def _best_cut(s: str, limit: int) -> int:
    """Rightmost acceptable break at or before ``limit``."""
    window = s[: limit + 1]
    for pattern in (_SENTENCE_END, _CLAUSE_END, _ANY_SPACE):
        found = [m.end() for m in pattern.finditer(window)]
        # Ignore breaks so early that they leave a stub line.
        usable = [p for p in found if p >= limit // 4]
        if usable:
            return usable[-1]
    return limit  # one unbroken run of characters — cut it


def repair_tables(text: str) -> str:
    """Drop all-empty columns and make every row the same width.

    Merged cells defeat the converters: a HWP budget table with rowspans came
    through as 19 rows of 33 columns that were 3% filled, and a header row of 4
    cells above a separator declaring 20. Markdown renderers mangle that, it
    reads as a table while carrying almost nothing, and the empty scaffolding
    (``|  |  |  |`` repeated) costs more tokens than the content.

    This is lossless: a column is removed only when *every* cell in it is empty,
    so no text can be dropped. A block left with one column is not a table at
    all and becomes plain lines instead.

    Well-formed tables come out byte-identical.
    """
    lines = text.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if not _is_table_row(lines[i]):
            out.append(lines[i])
            i += 1
            continue

        j = i
        block: list[str] = []
        while j < n and _is_table_row(lines[j]):
            block.append(lines[j])
            j += 1
        i = j
        out.extend(_repair_block(block))
    return "\n".join(out)


def _repair_block(block: list[str]) -> list[str]:
    rows = [(_cells(r), _is_separator_row(r)) for r in block]
    data = [c for c, sep in rows if not sep]
    if len(data) < 2:
        return block

    width = max(len(c) for c in data)
    padded = [c + [""] * (width - len(c)) for c in data]
    header, body_rows = padded[0], padded[1:]

    # Judge a column by its *data*, not its header. A column whose header has
    # text but whose every data cell is empty is not a column at all: the HWP
    # converter merged consecutive paragraphs into one row, so that "header"
    # holds whole sections of the document (8. 성과활용방안, 9. 소요예산 …).
    keep = [k for k in range(width) if any(r[k].strip() for r in body_rows)]

    if not keep:
        return block  # nothing but a header; strip_empty_tables decides
    if len(keep) == width and all(len(c) == width for c, _ in rows):
        return block  # already consistent — leave it exactly as it was

    # Header text over a dataless column is prose. Keep it, above the table.
    stray = [
        header[k].strip() for k in range(width) if k not in keep and header[k].strip()
    ]

    if len(keep) == 1:
        # One real column is a list, not a table.
        kept_lines = [r[keep[0]].strip() for r in padded if r[keep[0]].strip()]
        return _spaced(stray + kept_lines)

    had_separator = any(sep for _, sep in rows)
    table = ["| " + " | ".join(r[k] for k in keep) + " |" for r in padded]
    if had_separator and len(table) > 1:
        table.insert(1, "| " + " | ".join("---" for _ in keep) + " |")
    return _spaced(stray) + table if stray else table


def _spaced(lines: list[str]) -> list[str]:
    """Blank-line separate paragraphs so markdown does not run them together."""
    out: list[str] = []
    for line in lines:
        out.append(line)
        out.append("")
    return out


_BRACKET_TITLE = re.compile(r"^\s*[<〈《\[]\s*(.+?)\s*[>〉》\]]\s*$")


@dataclass
class TitleResult:
    text: str
    title: str | None


def promote_title(text: str) -> TitleResult:
    """Determine the document title from its first substantive line and make
    sure that line is an H1.

    The FIRST real line wins — not the first ``# `` found anywhere — because
    extraction often mis-promotes a mid-document note to H1 while the true
    title sits as plain text at the very top (a banner like ``< … >`` or a
    cover line). Skips blank lines, HTML comments, images, and leading tables.
    """
    lines = text.splitlines()

    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s or s.startswith("![") or s.startswith("<!--") or _is_table_row(s):
            continue

        if s.startswith("#"):
            # First real line is already a heading: trust its text as the title.
            title = s.lstrip("#").strip()
            return TitleResult(text, title or None)

        # First real line is plain text — this is the title banner/cover.
        m = _BRACKET_TITLE.match(s)
        title = (m.group(1) if m else s).strip()
        if 2 <= len(title) <= 80:
            lines[i] = f"# {title}"
            return TitleResult("\n".join(lines), title)
        return TitleResult(text, None)

    return TitleResult(text, None)


@dataclass
class CleanResult:
    body: str
    title: str | None
    headings: structure.HeadingReport


def clean(body: str) -> CleanResult:
    body = strip_image_refs(body)
    body = collapse_glyph_runs(body)
    # Repair before stripping: a table that collapses to nothing here is then
    # removed as empty rather than left as a husk of separators.
    body = repair_tables(body)
    body = strip_empty_tables(body)
    # Last, so wrapping sees the final text and nothing re-joins the lines.
    body = wrap_long_lines(body)
    titled = promote_title(body)
    # Title discovery only guarantees the *first* line is a heading. Enforcing
    # the invariants the wiki builder needs (one H1, a non-empty H2, no level
    # jumps) is structure.py's job.
    normalized = structure.normalize_headings(titled.text, titled.title)
    return CleanResult(
        body=normalized.body,
        title=titled.title,
        headings=normalized.report,
    )
