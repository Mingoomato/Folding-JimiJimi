"""Split a converted document into LLM-sized chunks.

A whole 입찰안내서 runs to hundreds of KB — far past what fits in one prompt. This
splits the markdown body into pieces small enough to hand to a model, while
keeping each piece *semantically* whole:

  - **Tables and code are never split.** A table cut in half is worse than an
    oversized chunk: the header row and its data end up in different prompts.
    Structure parsing just fought to keep those tables intact; splitting them
    here would throw that away.
  - **Splits prefer heading boundaries**, so a chunk is a section rather than an
    arbitrary window.
  - **Each chunk carries its heading breadcrumb**, so a chunk pulled out of the
    middle of a document still says which section it came from.

Size is measured in characters, not tokens: a tokenizer would be another
dependency and every model tokenizes differently. For Korean, 1 char is roughly
0.5-1 token, so the default 4000 chars lands well inside a typical context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MAX_CHARS = 4000
# A heading only starts a new chunk once the current one is already substantial.
# With a low floor (500) every heading cut the chunk short: on a real 710K-char
# 입찰안내서 whose converter emitted 771 spurious H1s, that produced 346 chunks
# averaging 2050 chars with 55% under 2000. At 60% of max_chars the same
# document yields 210 chunks averaging 3379 with none under 1000 — sections stay
# whole instead of being sliced at every stray heading.
DEFAULT_MIN_CHARS = 2400
# Headings at or above this level (h1, h2) are preferred chunk boundaries.
DEFAULT_SPLIT_LEVEL = 2

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_HTML_TABLE_OPEN = re.compile(r"<table\b", re.IGNORECASE)
_HTML_TABLE_CLOSE = re.compile(r"</table>", re.IGNORECASE)
# Sentence-ish boundaries used only when a single prose block is oversized.
_SENTENCE_END = re.compile(r"(?<=[.!?。？！])\s+|(?<=[다요])\.\s+")


@dataclass
class Block:
    kind: str  # "heading" | "table" | "code" | "text"
    text: str
    level: int = 0  # heading level, 0 for non-headings
    splittable: bool = True


@dataclass
class Chunk:
    index: int
    body: str
    heading_path: list[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return len(self.body)


def parse_blocks(md: str) -> list[Block]:
    """Group the markdown into atomic blocks; tables and code stay whole."""
    lines = md.splitlines()
    blocks: list[Block] = []
    buf: list[str] = []

    def flush_text() -> None:
        if buf:
            text = "\n".join(buf).strip("\n")
            if text.strip():
                blocks.append(Block("text", text))
            buf.clear()

    i, n = 0, len(lines)
    while i < n:
        line = lines[i]

        # fenced code — consume through the closing fence
        m = _FENCE.match(line)
        if m:
            flush_text()
            fence = m.group(1)
            block = [line]
            i += 1
            while i < n:
                block.append(lines[i])
                if lines[i].strip().startswith(fence):
                    i += 1
                    break
                i += 1
            blocks.append(Block("code", "\n".join(block), splittable=False))
            continue

        # HTML table (emitted for merged-cell tables) — consume to </table>
        if _HTML_TABLE_OPEN.search(line):
            flush_text()
            block = [line]
            closed = bool(_HTML_TABLE_CLOSE.search(line))
            i += 1
            while i < n and not closed:
                block.append(lines[i])
                closed = bool(_HTML_TABLE_CLOSE.search(lines[i]))
                i += 1
            blocks.append(Block("table", "\n".join(block), splittable=False))
            continue

        # markdown pipe table — consume consecutive rows
        if _TABLE_ROW.match(line):
            flush_text()
            block = []
            while i < n and _TABLE_ROW.match(lines[i]):
                block.append(lines[i])
                i += 1
            blocks.append(Block("table", "\n".join(block), splittable=False))
            continue

        m = _HEADING.match(line)
        if m:
            flush_text()
            blocks.append(Block("heading", line, level=len(m.group(1))))
            i += 1
            continue

        if not line.strip():
            flush_text()
            i += 1
            continue

        buf.append(line)
        i += 1

    flush_text()
    return blocks


def _split_oversized_text(text: str, max_chars: int) -> list[str]:
    """Last resort for a single prose block bigger than the budget."""
    parts: list[str] = []
    for piece in _SENTENCE_END.split(text):
        if not piece:
            continue
        if parts and len(parts[-1]) + len(piece) + 1 <= max_chars:
            parts[-1] = f"{parts[-1]} {piece}"
        elif len(piece) <= max_chars:
            parts.append(piece)
        else:
            # still too long (no sentence breaks) — hard-wrap it
            for k in range(0, len(piece), max_chars):
                parts.append(piece[k : k + max_chars])
    return parts or [text]


def chunk_markdown(
    body: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    min_chars: int = DEFAULT_MIN_CHARS,
    split_level: int = DEFAULT_SPLIT_LEVEL,
) -> list[Chunk]:
    """Split ``body`` into chunks, returning at least one chunk for any input."""
    blocks = parse_blocks(body)
    if not blocks:
        return [Chunk(index=1, body=body.strip())]

    chunks: list[Chunk] = []
    cur: list[tuple[str, bool]] = []  # (text, is_heading)
    cur_len = 0
    heading_stack: list[tuple[int, str]] = []
    cur_path: list[str] = []

    def flush() -> None:
        """Close the current chunk, never orphaning a trailing heading.

        A chunk that ends on "## 제3장 참가자격" leaves that section's title in
        one chunk and its content in the next — the retrieved text then has no
        idea what it is about. Trailing headings are carried into the next chunk
        so a heading always travels with the content it introduces.
        """
        nonlocal cur, cur_len, cur_path
        carry: list[tuple[str, bool]] = []
        while cur and cur[-1][1]:
            carry.insert(0, cur.pop())

        text = "\n\n".join(t for t, _ in cur).strip()
        if text:
            chunks.append(
                Chunk(index=len(chunks) + 1, body=text, heading_path=list(cur_path))
            )
            cur_path = [t for _, t in heading_stack]
        else:
            carry = cur + carry  # nothing but headings: keep them together

        cur = carry
        cur_len = sum(len(t) + 2 for t, _ in cur)

    for block in blocks:
        if block.kind == "heading":
            # maintain the breadcrumb stack
            while heading_stack and heading_stack[-1][0] >= block.level:
                heading_stack.pop()
            title = _HEADING.match(block.text).group(2).strip()

            # a top-level heading starts a fresh section once we have enough
            if block.level <= split_level and cur_len >= min_chars:
                flush()
            if not cur:
                cur_path = [t for _, t in heading_stack]
            heading_stack.append((block.level, title))

            cur.append((block.text, True))
            cur_len += len(block.text) + 2
            continue

        pieces = (
            [block.text]
            if not block.splittable or len(block.text) <= max_chars
            else _split_oversized_text(block.text, max_chars)
        )

        for piece in pieces:
            if cur_len + len(piece) > max_chars and cur_len >= min_chars:
                flush()
            if not cur:
                cur_path = [t for _, t in heading_stack]
            cur.append((piece, False))
            cur_len += len(piece) + 2

    flush()
    if cur:  # headings carried past the final flush
        text = "\n\n".join(t for t, _ in cur).strip()
        if text:
            chunks.append(
                Chunk(index=len(chunks) + 1, body=text, heading_path=list(cur_path))
            )
    if not chunks:
        chunks = [Chunk(index=1, body=body.strip())]
    return chunks


def render_chunk(chunk: Chunk) -> str:
    """Chunk body prefixed with its heading breadcrumb, when it has one."""
    if not chunk.heading_path:
        return chunk.body
    crumb = " > ".join(chunk.heading_path)
    return f"<!-- section: {crumb} -->\n\n{chunk.body}"


def build_document_chunks(
    path: Path, doc_fm, body: str, opts
) -> list:
    """Split the body and give every chunk its own usable frontmatter.

    A chunk shares the document's ``id`` (it is the same document) but gets its
    own ``chunk_no``, so a retrieved chunk can always be traced back to its
    source document and position.
    """
    from app import frontmatter
    from app.schemas import DocumentChunk

    max_chars, min_chars = opts.resolve(len(body))
    pieces = chunk_markdown(
        body,
        max_chars=max_chars,
        min_chars=min_chars,
        split_level=opts.split_level,
    )

    slug = frontmatter.slugify(path.name)
    out: list[DocumentChunk] = []
    for piece in pieces:
        chunk_no = f"{slug}_{piece.index:03d}"
        chunk_fm = doc_fm.model_copy(update={"chunk_no": chunk_no})
        rendered = render_chunk(piece)
        out.append(
            DocumentChunk(
                chunk_no=chunk_no,
                index=piece.index,
                markdown=frontmatter.render(chunk_fm, rendered),
                body=rendered,
                frontmatter=chunk_fm,
                heading_path=piece.heading_path,
                chars=piece.chars,
            )
        )
    return out
