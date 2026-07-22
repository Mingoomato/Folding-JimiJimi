from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Protocol

from codegate_api.documents.writers.base import WriterError


class TextRun(Protocol):
    text: str | None


def replace_across_runs(runs: Sequence[TextRun], expected: str, replacement: str) -> None:
    text = "".join(run.text or "" for run in runs)
    if text.count(expected) != 1:
        raise WriterError(
            "expected_value_mismatch",
            "locator content does not contain expected text exactly once",
        )
    start = text.index(expected)
    end = start + len(expected)
    spans: list[tuple[int, int]] = []
    cursor = 0
    for run in runs:
        next_cursor = cursor + len(run.text or "")
        spans.append((cursor, next_cursor))
        cursor = next_cursor
    start_index = next(
        (index for index, (_, right) in enumerate(spans) if right > start),
        len(spans) - 1,
    )
    end_index = next(
        (index for index, (_, right) in enumerate(spans) if right >= end),
        len(spans) - 1,
    )
    start_left, _ = spans[start_index]
    end_left, _ = spans[end_index]
    start_run = runs[start_index]
    end_run = runs[end_index]
    prefix = (start_run.text or "")[: start - start_left]
    suffix = (end_run.text or "")[end - end_left :]
    start_run.text = prefix + replacement + (suffix if start_index == end_index else "")
    for index in range(start_index + 1, end_index):
        runs[index].text = ""
    if end_index != start_index:
        end_run.text = suffix


def markdown_blocks(markdown: str) -> list[tuple[str, str | list[str]]]:
    blocks: list[tuple[str, str | list[str]]] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(("paragraph", " ".join(paragraph).strip()))
            paragraph.clear()

    for raw_line in markdown.replace("\r\n", "\n").split("\n"):
        line = raw_line.rstrip()
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        bullet = re.match(r"^\s*[-*+]\s+(.+)$", line)
        if heading:
            flush_paragraph()
            blocks.append((f"heading{len(heading.group(1))}", heading.group(2).strip()))
        elif bullet:
            flush_paragraph()
            blocks.append(("bullet", bullet.group(1).strip()))
        elif not line.strip():
            flush_paragraph()
        else:
            paragraph.append(line.strip())
    flush_paragraph()
    return blocks
