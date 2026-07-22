"""OCR of embedded pictures, spliced back where the picture sat.

The rule these protect: **the text converter owns the body, OCR only fills in
pictures.** Reading a rendered page instead re-read text the converter already
had — 80% of recognised lines were duplicates — and flattened tables into loose
fragments.
"""

from __future__ import annotations

import pytest

from app import converters
from app.images import EmbeddedImage


def _img(order, **kw):
    return EmbeddedImage(data=b"x", order=order, **kw)


# --------------------------------------------------------------------------
# duplicate suppression
# --------------------------------------------------------------------------


def test_lines_already_in_the_body_are_dropped():
    body_flat = converters._WS.sub("", "# Value\n\nHUMAN ADD\nCEO Chaejeong Heo\n")
    ocr = "Value\nHUMAN\nADD\nChaejeong Heo\n사진 속에만 있는 문구"

    assert converters._novel_lines(ocr, body_flat) == "사진 속에만 있는 문구"


def test_comparison_ignores_whitespace():
    """OCR breaks lines where the layout does; the converter does not."""
    body_flat = converters._WS.sub("", "AI 기반 바이오 소재 융복합 제품")
    assert converters._novel_lines("AI기반  바이오\n소재 융복합 제품", body_flat) == ""


def test_single_character_fragments_are_dropped():
    assert converters._novel_lines("가\n나\n의미 있는 문장", "") == "의미 있는 문장"


def test_nothing_novel_yields_nothing():
    body_flat = converters._WS.sub("", "전부 이미 있는 내용")
    assert converters._novel_lines("전부 이미 있는 내용", body_flat) == ""


# --------------------------------------------------------------------------
# pptx: replace markitdown's placeholder in place
# --------------------------------------------------------------------------


def test_pptx_ocr_replaces_the_image_reference_in_place():
    body = "제목\n\n![](그림90.jpg)\n\n다음 문단"
    found = [_img(0, placeholder="그림90.jpg")]
    out = converters._splice_by_placeholder(body, found, {0: "사진 속 문구"})

    assert "![](그림90.jpg)" not in out
    assert "사진 속 문구" in out
    assert out.index("제목") < out.index("사진 속 문구") < out.index("다음 문단")


def test_pptx_shape_name_maps_to_markitdowns_filename():
    """Mirrors markitdown: re.sub(r"\\W", "", shape.name) + ".jpg"."""
    from app.images import _shape_ref

    assert _shape_ref("그림 90") == "그림90.jpg"
    assert _shape_ref("Picture 7") == "Picture7.jpg"


def test_repeated_shape_names_consume_references_left_to_right():
    body = "![](로고.jpg)\n\n가운데\n\n![](로고.jpg)"
    found = [_img(0, placeholder="로고.jpg"), _img(1, placeholder="로고.jpg")]
    out = converters._splice_by_placeholder(body, found, {0: "첫번째", 1: "두번째"})

    assert out.index("첫번째") < out.index("가운데") < out.index("두번째")


def test_image_without_recognised_text_keeps_its_reference():
    """postprocess strips the leftover; splicing must not invent content."""
    body = "![](그림1.jpg)"
    assert converters._splice_by_placeholder(body, [_img(0, placeholder="그림1.jpg")], {}) == body


# --------------------------------------------------------------------------
# hwpx: insert after the anchor text
# --------------------------------------------------------------------------


def test_hwpx_ocr_is_inserted_after_its_anchor():
    body = "앞 문단입니다.\n\n(3) 위치도\n\n뒤 문단입니다."
    found = [_img(0, anchor="(3) 위치도")]
    out = converters._splice_by_anchor(body, found, {0: "위치도 안의 글자"})

    assert out.index("(3) 위치도") < out.index("위치도 안의 글자")
    assert out.index("위치도 안의 글자") < out.index("뒤 문단입니다")


def test_hwpx_anchor_matches_despite_different_spacing():
    """HWPX splits one run across many <hp:t>, so spacing never matches."""
    body = "송파하남선 광역철도 1공구 건설공사\n\n다음 내용"
    found = [_img(0, anchor="송파하남선광역철도1공구건설공사")]
    out = converters._splice_by_anchor(body, found, {0: "도면 텍스트"})

    assert "도면 텍스트" in out


def test_hwpx_unmatched_anchor_is_skipped_not_guessed():
    body = "전혀 다른 내용"
    out = converters._splice_by_anchor(body, [_img(0, anchor="없는 앵커")], {0: "무언가"})

    assert out == body


def test_hwpx_multiple_insertions_keep_their_order():
    body = "첫째 구간\n\n둘째 구간\n\n셋째 구간"
    found = [_img(0, anchor="첫째 구간"), _img(1, anchor="둘째 구간")]
    out = converters._splice_by_anchor(body, found, {0: "AAA", 1: "BBB"})

    assert out.index("AAA") < out.index("둘째 구간") < out.index("BBB")


# --------------------------------------------------------------------------
# pdf: attach to the page the image sits on
# --------------------------------------------------------------------------


def test_pdf_ocr_lands_on_its_own_page():
    body = (
        f"{converters.page_marker(1)}\n\n1쪽 본문\n\n"
        f"{converters.page_marker(2)}\n\n2쪽 본문\n\n"
        f"{converters.page_marker(3)}\n\n3쪽 본문"
    )
    found = [_img(0, page=2)]
    out = converters._splice_by_page(body, found, {0: "2쪽 그림 텍스트"})

    after_p2 = out.index(converters.page_marker(2))
    after_p3 = out.index(converters.page_marker(3))
    assert after_p2 < out.index("2쪽 그림 텍스트") < after_p3


def test_pdf_several_images_on_one_page_are_merged():
    body = f"{converters.page_marker(1)}\n\n본문"
    found = [_img(0, page=1), _img(1, page=1)]
    out = converters._splice_by_page(body, found, {0: "위쪽 그림", 1: "아래쪽 그림"})

    assert "위쪽 그림" in out and "아래쪽 그림" in out
    assert out.count("이미지 텍스트 (OCR)") == 1


def test_pdf_body_without_markers_is_left_alone():
    body = "페이지 마커가 없는 본문"
    assert converters._splice_by_page(body, [_img(0, page=1)], {0: "x"}) == body


# --------------------------------------------------------------------------
# format policy
# --------------------------------------------------------------------------


def test_legacy_hwp_reports_that_images_cannot_be_read(tmp_path):
    """A text-only result must not look complete when images were dropped."""
    from app.errors import diagnostic

    d = diagnostic("hwp_images_unsupported")
    assert d.severity == "info"
    assert d.retryable is False


@pytest.mark.parametrize("n", [1, 7, 353])
def test_page_marker_round_trips(n):
    from app.structure import _PAGE_MARKER

    assert int(_PAGE_MARKER.search(converters.page_marker(n)).group(1)) == n


# --------------------------------------------------------------------------
# letterless fragments
# --------------------------------------------------------------------------


def test_letterless_lines_are_dropped():
    """A 2.3MB photo recognised as [':', '208'], reused in ten places, put a
    meaningless '208' into the body ten times."""
    assert converters._novel_lines("208\n:\n2026", "") == ""


def test_digits_inside_a_real_phrase_survive():
    assert converters._novel_lines("10,000천원", "") == "10,000천원"
    assert converters._novel_lines("P1 프로그램", "") == "P1 프로그램"


def test_short_latin_tokens_survive():
    assert converters._novel_lines("AI\nP1", "") == "AI\nP1"


def test_mixed_block_keeps_only_the_meaningful_lines():
    ocr = "208\n조직 재생 연구\n:\n99"
    assert converters._novel_lines(ocr, "") == "조직 재생 연구"
