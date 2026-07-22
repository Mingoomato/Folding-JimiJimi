"""Heading structure the downstream wiki builder can rely on.

The consumer generates stable ``sec-*`` anchors from the H1/H2 sequence, and
refuses to publish a document whose structure it cannot map. That imposes three
invariants on our output:

  1. **exactly one H1** — it is the document title
  2. **at least one H2 with a body** — anchors need something to point at
  3. **no level jump greater than one** — H1 → H3 leaves no section to anchor

Real converter output violates all three. One 710K-char hwpx emits 771 H1s
because every numbered clause in the source was styled as a title; scanned PDFs
emit no headings at all.

Everything here is deterministic: the same body always produces the same
headings in the same order. That is the whole basis for anchor stability — the
consumer's ``sec-004`` keeps meaning the same section only because we do not
reshuffle levels between runs.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")

MAX_LEVEL = 6
# Used when a document has body text but no H2 to hang it on.
SYNTHETIC_H2 = "본문"


@dataclass
class HeadingReport:
    """What had to be corrected, so it can surface as a diagnostic rather than
    a silent edit."""

    demoted_h1: int = 0
    releveled: int = 0
    shifted_to_h1: bool = False
    synthesized_h1: bool = False
    synthesized_h2: bool = False
    h1_count: int = 0
    h2_count: int = 0
    nonempty_h2_count: int = 0

    @property
    def changed(self) -> bool:
        return bool(
            self.demoted_h1
            or self.releveled
            or self.shifted_to_h1
            or self.synthesized_h1
            or self.synthesized_h2
        )

    def messages(self) -> list[str]:
        out: list[str] = []
        if self.synthesized_h1:
            out.append("문서에 제목(H1)이 없어 생성했습니다")
        if self.shifted_to_h1:
            out.append("최상위 heading이 H1이 아니어서 전체 계층을 올렸습니다")
        if self.demoted_h1:
            out.append(f"중복 H1 {self.demoted_h1}개를 H2로 강등했습니다")
        if self.releveled:
            out.append(f"heading 레벨 점프 {self.releveled}곳을 보정했습니다")
        if self.synthesized_h2:
            out.append(f"본문을 담을 H2가 없어 '## {SYNTHETIC_H2}'를 생성했습니다")
        return out


@dataclass
class NormalizeResult:
    body: str
    report: HeadingReport = field(default_factory=HeadingReport)


@dataclass
class _Heading:
    line: int
    level: int
    text: str


def iter_heading_lines(lines: list[str]):
    """Yield ``(index, level, text)`` for real ATX headings only.

    A ``# comment`` inside a fenced code block is not a heading; treating it as
    one would relevel code and corrupt the fence's contents.
    """
    fence: str | None = None
    for i, line in enumerate(lines):
        m = _FENCE.match(line)
        if m:
            token = m.group(1)
            if fence is None:
                fence = token
            elif line.strip().startswith(fence):
                fence = None
            continue
        if fence is not None:
            continue
        h = _HEADING.match(line)
        if h:
            yield i, len(h.group(1)), h.group(2).strip()


def _collect(lines: list[str]) -> list[_Heading]:
    return [_Heading(i, lvl, txt) for i, lvl, txt in iter_heading_lines(lines)]


def _clamp(level: int) -> int:
    return max(1, min(MAX_LEVEL, level))


def _enforce_single_h1(headings: list[_Heading], report: HeadingReport) -> None:
    """Make the first heading an H1 and demote every later H1 to H2.

    A demoted H1 takes its whole subtree with it — the H2s under it become H3s —
    so the section nesting the author intended survives the flattening. Without
    that shift, ``# 제1장`` / ``## 1절`` would collapse into two sibling H2s and
    the clause would look like a chapter.
    """
    if not headings:
        return

    # Lift (or lower) everything uniformly so the document opens at H1.
    delta = 1 - headings[0].level
    if delta:
        report.shifted_to_h1 = True
        for h in headings:
            h.level = _clamp(h.level + delta)

    shift = 0
    seen_h1 = False
    for h in headings:
        if h.level == 1:
            if seen_h1:
                shift = 1
                report.demoted_h1 += 1
            else:
                seen_h1 = True
                shift = 0
        h.level = _clamp(h.level + shift)


def _enforce_monotonic(headings: list[_Heading], report: HeadingReport) -> None:
    """Forbid level jumps greater than one (H1 → H3 becomes H1 → H2).

    Safe to run after :func:`_enforce_single_h1`: the first heading is at level
    1, and ``prev + 1`` can only equal 1 when ``prev`` is 0, so no second H1 can
    be introduced here.
    """
    prev = 0
    for h in headings:
        if h.level > prev + 1:
            h.level = prev + 1
            report.releveled += 1
        prev = h.level


def _is_body_line(line: str) -> bool:
    s = line.strip()
    return bool(s) and not _HEADING.match(line)


def _nonempty_h2_lines(lines: list[str], headings: list[_Heading]) -> set[int]:
    """Line numbers of H2s that own at least one line of body text.

    A section runs until the next H1 or H2, so text sitting under an H3 child
    still counts as its parent H2's body.
    """
    out: set[int] = set()
    for idx, h in enumerate(headings):
        if h.level != 2:
            continue
        end = len(lines)
        for later in headings[idx + 1 :]:
            if later.level <= 2:
                end = later.line
                break
        if any(_is_body_line(lines[i]) for i in range(h.line + 1, end)):
            out.add(h.line)
    return out


def normalize_headings(body: str, title: str | None = None) -> NormalizeResult:
    """Rewrite ``body`` so its headings satisfy the consumer's invariants."""
    lines = body.splitlines()
    report = HeadingReport()

    headings = _collect(lines)

    # No heading anywhere: the document still needs a title to anchor to.
    if not headings:
        if not any(_is_body_line(ln) for ln in lines):
            return NormalizeResult(body, report)
        heading = f"# {(title or '문서').strip()}"
        lines = [heading, ""] + lines
        report.synthesized_h1 = True
        headings = _collect(lines)

    _enforce_single_h1(headings, report)
    _enforce_monotonic(headings, report)

    for h in headings:
        lines[h.line] = f"{'#' * h.level} {h.text}".rstrip()

    nonempty = _nonempty_h2_lines(lines, headings)
    if not nonempty:
        insert_at = _first_body_line_after_h1(lines, headings)
        if insert_at is not None:
            lines[insert_at:insert_at] = [f"## {SYNTHETIC_H2}", ""]
            report.synthesized_h2 = True
            headings = _collect(lines)
            nonempty = _nonempty_h2_lines(lines, headings)

    report.h1_count = sum(1 for h in headings if h.level == 1)
    report.h2_count = sum(1 for h in headings if h.level == 2)
    report.nonempty_h2_count = len(nonempty)
    return NormalizeResult("\n".join(lines), report)


def structural_diagnostics(report: HeadingReport) -> list:
    """Diagnostics describing what normalization did, or could not do.

    Shared by the API and the batch CLI so a manifest and a /v2/convert response
    never disagree about the same document.
    """
    from app.errors import diagnostic

    out = []
    if report.changed:
        out.append(diagnostic("heading_normalized", detail="; ".join(report.messages())))
    if report.nonempty_h2_count == 0:
        # We could not satisfy "at least one non-empty H2" — a document whose
        # entire content is its title leaves nothing to put under a section.
        # Say so, rather than shipping a body the consumer will refuse anyway.
        out.append(diagnostic("structure_incomplete"))
    return out


def _first_body_line_after_h1(
    lines: list[str], headings: list[_Heading]
) -> int | None:
    """Where to splice a synthetic H2 so it actually gets a body."""
    start = headings[0].line + 1 if headings else 0
    for i in range(start, len(lines)):
        if _is_body_line(lines[i]):
            return i
    return None


# ---------------------------------------------------------------------------
# Section mapping
# ---------------------------------------------------------------------------

# Page/slide provenance the converters emit. PDFs get `<!--- page N --->` on
# every page boundary (see converters.page_marker); markitdown writes the slide
# marker for every pptx deck. The `-*` allows either comment style.
_PAGE_MARKER = re.compile(r"<!--+\s*page\s+(\d+)\s*-+->", re.IGNORECASE)
_SLIDE_MARKER = re.compile(r"<!--\s*Slide number:\s*(\d+)\s*-->", re.IGNORECASE)


@dataclass
class Section:
    """One anchorable region of the canonical body.

    Only H1/H2 produce sections, because that is what the consumer builds its
    ``sec-*`` anchors from. Deeper headings live inside their parent's span.
    """

    ordinal: int
    anchor_hint: str
    stable_key: str
    level: int
    heading: str
    heading_path: list[str]
    char_start: int
    char_end: int
    source_page: int | None


def _line_offsets(body: str) -> list[int]:
    """Character offset of the start of each line."""
    offsets = [0]
    for line in body.split("\n")[:-1]:
        offsets.append(offsets[-1] + len(line) + 1)
    return offsets


def _page_at_line(lines: list[str]) -> list[int | None]:
    """Carry the most recent page/slide marker forward over every line."""
    out: list[int | None] = []
    current: int | None = None
    for line in lines:
        m = _PAGE_MARKER.search(line) or _SLIDE_MARKER.search(line)
        if m:
            current = int(m.group(1))
        out.append(current)
    return out


def _stable_key(heading_path: list[str]) -> str:
    """Content-derived identity that survives edits elsewhere in the document.

    ``anchor_hint`` mirrors the consumer's ordinal numbering, but an ordinal
    shifts the moment a section is inserted above it — ``sec-004`` in a new
    build may be ``sec-003`` from the old one. Hashing the heading path instead
    lets the consumer recognise the same section across revisions, which is what
    "stable across changes" in the integration contract actually requires.
    """
    joined = " > ".join(heading_path)
    return "h-" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:8]


def extract_sections(body: str) -> list[Section]:
    """Map the canonical body's H1/H2 regions. Run *after* normalize_headings."""
    lines = body.split("\n")
    offsets = _line_offsets(body)
    pages = _page_at_line(lines)

    all_headings = _collect(lines)
    anchors = [h for h in all_headings if h.level <= 2]
    if not anchors:
        return []

    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    seen: dict[str, int] = {}

    for i, h in enumerate(anchors):
        while stack and stack[-1][0] >= h.level:
            stack.pop()
        stack.append((h.level, h.text))
        path = [text for _, text in stack]

        key = _stable_key(path)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            key = f"{key}-{seen[key]}"

        end_line = anchors[i + 1].line if i + 1 < len(anchors) else len(lines)
        char_end = offsets[end_line] if end_line < len(offsets) else len(body)

        sections.append(
            Section(
                ordinal=i + 1,
                anchor_hint=f"sec-{i + 1:03d}",
                stable_key=key,
                level=h.level,
                heading=h.text,
                heading_path=path,
                char_start=offsets[h.line],
                char_end=char_end,
                source_page=pages[h.line],
            )
        )
    return sections
