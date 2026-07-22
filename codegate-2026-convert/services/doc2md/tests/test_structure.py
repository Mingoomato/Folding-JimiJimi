"""The heading invariants the downstream wiki builder refuses to publish without."""

from __future__ import annotations

import pytest

from app.structure import (
    SYNTHETIC_H2,
    iter_heading_lines,
    normalize_headings,
)


def levels(body: str) -> list[int]:
    return [lvl for _, lvl, _ in iter_heading_lines(body.splitlines())]


def headings(body: str) -> list[tuple[int, str]]:
    return [(lvl, txt) for _, lvl, txt in iter_heading_lines(body.splitlines())]


def assert_invariants(body: str) -> None:
    """Exactly one H1, at least one non-empty H2, no level jump > 1."""
    lvls = levels(body)
    assert lvls.count(1) == 1, f"expected exactly one H1, got {lvls.count(1)}"
    assert 2 in lvls, "expected at least one H2"
    prev = 0
    for lvl in lvls:
        assert lvl <= prev + 1, f"level jump {prev} -> {lvl}"
        prev = lvl


# --------------------------------------------------------------------------
# single H1
# --------------------------------------------------------------------------


def test_extra_h1s_are_demoted_and_their_subtree_moves_with_them():
    body = (
        "# 입찰안내서\n\n서문\n\n"
        "# 제1장 총칙\n\n조문\n\n"
        "## 제1절 목적\n\n목적 본문\n\n"
        "# 제2장 입찰\n\n조문2\n"
    )
    out = normalize_headings(body)

    assert headings(out.body) == [
        (1, "입찰안내서"),
        (2, "제1장 총칙"),
        (3, "제1절 목적"),
        (2, "제2장 입찰"),
    ]
    assert out.report.demoted_h1 == 2
    assert_invariants(out.body)


def test_the_771_h1_case_collapses_to_one():
    """A real 710K-char hwpx styled every clause as a title."""
    body = "# 문서 제목\n\n서문\n\n" + "".join(
        f"# 제{i}조\n\n제{i}조 본문\n\n" for i in range(1, 772)
    )
    out = normalize_headings(body)

    assert out.report.demoted_h1 == 771
    assert levels(out.body).count(1) == 1
    assert levels(out.body).count(2) == 771
    assert_invariants(out.body)


def test_document_starting_below_h1_is_lifted_preserving_depth():
    body = "## 개요\n\n내용\n\n### 세부\n\n세부 내용\n"
    out = normalize_headings(body)

    assert headings(out.body) == [(1, "개요"), (2, "세부")]
    assert out.report.shifted_to_h1
    assert_invariants(out.body)


# --------------------------------------------------------------------------
# monotonic levels
# --------------------------------------------------------------------------


def test_level_jump_is_closed_up():
    body = "# 제목\n\n서문\n\n#### 갑자기 깊은 절\n\n본문\n"
    out = normalize_headings(body)

    assert headings(out.body) == [(1, "제목"), (2, "갑자기 깊은 절")]
    assert out.report.releveled == 1
    assert_invariants(out.body)


# --------------------------------------------------------------------------
# non-empty H2
# --------------------------------------------------------------------------


def test_h2_is_synthesized_when_the_document_has_only_a_title():
    body = "# 제목\n\n본문만 있고 절이 없다.\n"
    out = normalize_headings(body)

    assert out.report.synthesized_h2
    assert headings(out.body) == [(1, "제목"), (2, SYNTHETIC_H2)]
    assert_invariants(out.body)


def test_a_h2_holding_only_a_h3_subsection_counts_as_non_empty():
    """Text under an H3 child is still its parent H2's section body."""
    body = "# 제목\n\n## 절\n\n### 항\n\n항 본문\n"
    out = normalize_headings(body)

    assert not out.report.synthesized_h2
    assert out.report.nonempty_h2_count == 1
    assert_invariants(out.body)


def test_empty_trailing_h2s_do_not_defeat_the_guarantee():
    body = "# 제목\n\n서문\n\n## 빈 절 1\n## 빈 절 2\n"
    out = normalize_headings(body)

    assert out.report.nonempty_h2_count >= 1
    assert_invariants(out.body)


# --------------------------------------------------------------------------
# safety
# --------------------------------------------------------------------------


def test_headings_inside_code_fences_are_left_alone():
    body = (
        "# 제목\n\n## 절\n\n예시:\n\n"
        "```python\n# 이것은 주석이지 heading이 아니다\n## 이것도\n```\n\n끝.\n"
    )
    out = normalize_headings(body)

    assert "# 이것은 주석이지 heading이 아니다" in out.body
    assert "## 이것도" in out.body
    assert headings(out.body) == [(1, "제목"), (2, "절")]


def test_already_valid_document_is_untouched():
    body = "# 제목\n\n## 절\n\n본문\n"
    out = normalize_headings(body)

    assert out.body.strip() == body.strip()
    assert not out.report.changed


def test_normalization_is_deterministic():
    """Anchor stability rests entirely on this."""
    body = "## a\n\nx\n\n# b\n\ny\n\n#### c\n\nz\n"
    first = normalize_headings(body).body
    assert normalize_headings(body).body == first
    # and idempotent: re-normalizing a normalized body changes nothing
    assert normalize_headings(first).body == first


@pytest.mark.parametrize(
    "body",
    ["", "   \n\n  \n", "본문만 있고 heading이 전혀 없다.\n"],
)
def test_degenerate_input_does_not_crash(body):
    normalize_headings(body, title="제목")


def test_headingless_document_gets_a_title_and_a_section():
    out = normalize_headings("스캔 PDF에서 나온 본문 한 줄.\n", title="스캔문서")

    assert out.report.synthesized_h1
    assert headings(out.body) == [(1, "스캔문서"), (2, SYNTHETIC_H2)]
    assert_invariants(out.body)
