"""Repair of tables the converters mangle on merged cells.

A HWP budget table with rowspans came through as 19 rows of 33 columns that
were 3% filled, with a 4-cell header above a separator declaring 20 — markdown
renderers mangle that, and the empty ``|  |  |`` scaffolding costs more tokens
than the content it carries.

The invariant these protect: **repair never loses a character.** Columns are
dropped only when every data cell in them is empty, and header text over such a
column is prose that gets moved out, not deleted.
"""

from __future__ import annotations

import re
from collections import Counter

from app.postprocess import repair_tables


def content(text: str) -> Counter:
    """Every non-scaffolding character, order-independent.

    Repair is allowed to *move* text — header prose over a dataless column ends
    up above the table — but never to lose any, so the guarantee is a multiset
    comparison, not string equality.
    """
    return Counter(re.sub(r"[|\-\s]+", "", text))


def test_all_empty_columns_are_dropped():
    md = (
        "| 항목 | 금액 |  |  |  |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| 재료비 | 1,000 |  |  |  |\n"
        "| 인건비 | 2,000 |  |  |  |\n"
    )
    out = repair_tables(md)

    assert "| 항목 | 금액 |" in out
    assert "|  |  |" not in out
    assert content(out) == content(md)


def test_separator_width_is_rebuilt_to_match():
    """The real defect: 2 header cells over a 6-column separator."""
    md = "| 항목 | 금액 |\n| --- | --- | --- | --- | --- | --- |\n| 재료비 | 1,000 |\n"
    out = repair_tables(md)
    rows = [r for r in out.splitlines() if r.startswith("|")]
    widths = {r.count("|") for r in rows}

    assert len(widths) == 1, f"rows disagree on width: {rows}"


def test_header_text_over_a_dataless_column_becomes_prose():
    """The HWP case: consecutive paragraphs merged into one table row."""
    md = (
        "| 구분 | 8. 성과활용방안 | 9. 소요예산 |\n"
        "| --- | --- | --- |\n"
        "| 지원금 |  |  |\n"
        "| 부담금 |  |  |\n"
    )
    out = repair_tables(md)

    assert "8. 성과활용방안" in out
    assert "9. 소요예산" in out
    # they are no longer pretending to be columns
    assert "| 8. 성과활용방안 |" not in out
    assert out.index("8. 성과활용방안") < out.index("지원금")
    assert content(out) == content(md)


def test_single_data_column_becomes_plain_lines():
    md = (
        "| 항목 |  |  |\n"
        "| --- | --- | --- |\n"
        "| 재료비 |  |  |\n"
        "| 인건비 |  |  |\n"
    )
    out = repair_tables(md)

    assert "|" not in out
    assert "재료비" in out and "인건비" in out
    assert content(out) == content(md)


def test_well_formed_table_is_untouched():
    md = (
        "| 항목 | 금액 | 비고 |\n"
        "| --- | --- | --- |\n"
        "| 재료비 | 1,000 | 견적서 |\n"
        "| 인건비 | 2,000 | - |\n"
    )
    # splitlines()/join drops a trailing newline, as elsewhere in postprocess
    assert repair_tables(md).splitlines() == md.splitlines()


def test_partially_filled_column_is_kept():
    """One value is still data; only wholly empty columns go."""
    md = (
        "| 항목 | 금액 | 비고 |\n"
        "| --- | --- | --- |\n"
        "| 재료비 | 1,000 |  |\n"
        "| 인건비 | 2,000 | 확정 |\n"
    )
    out = repair_tables(md)

    assert "확정" in out
    assert out.count("비고") == 1


def test_text_outside_tables_is_untouched():
    md = "# 제목\n\n문단입니다.\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n끝."
    out = repair_tables(md)

    assert out.startswith("# 제목")
    assert out.endswith("끝.")


def test_repair_is_idempotent():
    md = "| 항목 | 금액 |  |  |\n| --- | --- | --- | --- |\n| 재료비 | 1,000 |  |  |\n"
    once = repair_tables(md)
    assert repair_tables(once) == once


def test_repair_never_loses_a_character_on_the_real_corpus():
    """The guarantee, checked against whatever the corpus currently holds."""
    from pathlib import Path

    out_dir = Path("output")
    if not out_dir.exists():
        return  # corpus not generated in this environment
    for f in sorted(out_dir.rglob("*.md")):
        body = f.read_text(encoding="utf-8")
        assert content(body) == content(repair_tables(body)), f.name


def test_office_lock_files_are_not_input(tmp_path):
    """Word/PowerPoint leave ~$name.pptx beside an open document: same
    extension, a few hundred bytes of bookkeeping, often unreadable."""
    from app.batch_cli import _is_document

    (tmp_path / "보고서.pptx").write_bytes(b"x")
    (tmp_path / "~$보고서.pptx").write_bytes(b"lock")
    (tmp_path / ".~lock.보고서.pptx#").write_bytes(b"lock")

    kept = sorted(p.name for p in tmp_path.iterdir() if _is_document(p))
    assert kept == ["보고서.pptx"]
