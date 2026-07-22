"""The v0.2 additions the integration contract asks doc2md to guarantee."""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from app.main import app

DOC = """# 입찰안내서

머리말 문단.

## 제1장 총칙

총칙 본문입니다.

## 제2장 참가자격

자격 본문입니다.
"""


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def source(tmp_path):
    p = tmp_path / "입찰안내서.md"
    p.write_text(DOC, encoding="utf-8")
    return p


def convert(client, source, **extra):
    payload = {"source": {"kind": "path", "path": str(source)}, **extra}
    r = client.post("/v2/convert", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------
# canonical / source SHA
# --------------------------------------------------------------------------


def test_source_and_canonical_sha_are_distinct_artifacts(client, source):
    out = convert(client, source)

    assert out["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert (
        out["canonical_sha256"]
        == hashlib.sha256(out["body"].encode("utf-8")).hexdigest()
    )
    assert out["source_sha256"] != out["canonical_sha256"]


def test_canonical_sha_ignores_frontmatter(client, source):
    """It hashes `body`, so it cannot depend on metadata the caller passes in."""
    a = convert(client, source)
    b = convert(client, source, metadata={"title": "완전히 다른 제목"})

    assert a["canonical_sha256"] == b["canonical_sha256"]
    assert a["frontmatter"]["title"] != b["frontmatter"]["title"]


# --------------------------------------------------------------------------
# converter provenance
# --------------------------------------------------------------------------


def test_converter_block_identifies_the_producer(client, source):
    conv = convert(client, source)["converter"]

    assert conv["name"] == "doc2md"
    assert conv["version"]
    assert conv["library"]


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------


def test_sections_cover_every_h1_and_h2(client, source):
    out = convert(client, source)
    sections = out["sections"]

    assert [s["heading"] for s in sections] == [
        "입찰안내서",
        "제1장 총칙",
        "제2장 참가자격",
    ]
    assert [s["level"] for s in sections] == [1, 2, 2]
    assert [s["anchor_hint"] for s in sections] == ["sec-001", "sec-002", "sec-003"]


def test_section_spans_slice_the_real_body(client, source):
    out = convert(client, source)
    body = out["body"]

    for s in out["sections"]:
        assert body[s["char_start"] : s["char_end"]].lstrip().startswith("#")
        assert s["heading"] in body[s["char_start"] : s["char_end"]]


def test_heading_path_is_a_breadcrumb(client, source):
    sections = convert(client, source)["sections"]

    assert sections[0]["heading_path"] == ["입찰안내서"]
    assert sections[1]["heading_path"] == ["입찰안내서", "제1장 총칙"]


def test_stable_key_survives_an_edit_that_shifts_ordinals(client, tmp_path):
    """The whole point of stable_key: `ordinal` shifts, identity must not."""
    before = tmp_path / "a.md"
    before.write_text(DOC, encoding="utf-8")
    original = convert(client, before)["sections"]

    # insert a new section ahead of 제2장, pushing its ordinal from 3 to 4
    after = tmp_path / "b.md"
    after.write_text(
        DOC.replace("## 제2장", "## 제1장의2 신설\n\n신설 본문.\n\n## 제2장"),
        encoding="utf-8",
    )
    revised = convert(client, after)["sections"]

    def find(sections, heading):
        return next(s for s in sections if s["heading"] == heading)

    old, new = find(original, "제2장 참가자격"), find(revised, "제2장 참가자격")
    assert old["ordinal"] != new["ordinal"], "expected the ordinal to shift"
    assert old["stable_key"] == new["stable_key"], "identity must survive the shift"


def test_duplicate_heading_paths_get_distinct_keys(client, tmp_path):
    p = tmp_path / "dup.md"
    p.write_text("# 문서\n\n## 별표\n\n하나.\n\n## 별표\n\n둘.\n", encoding="utf-8")
    keys = [s["stable_key"] for s in convert(client, p)["sections"]]

    assert len(keys) == len(set(keys))


def test_source_page_is_null_for_formats_without_pages(client, source):
    for s in convert(client, source)["sections"]:
        assert s["source_page"] is None


# --------------------------------------------------------------------------
# determinism — anchor stability rests on this
# --------------------------------------------------------------------------


def test_conversion_is_deterministic(client, source):
    a, b = convert(client, source), convert(client, source)

    assert a["canonical_sha256"] == b["canonical_sha256"]
    assert a["sections"] == b["sections"]


# --------------------------------------------------------------------------
# diagnostics
# --------------------------------------------------------------------------


@pytest.fixture
def unstructured(tmp_path):
    """A document the converter gave no headings at all — a scanned PDF's shape."""
    p = tmp_path / "구조없음.md"
    p.write_text(
        "스캔 문서에서 나온 첫 줄.\n\n두 번째 문단.\n\n세 번째 문단.\n", encoding="utf-8"
    )
    return p


def test_diagnostics_carry_a_retryable_flag(client, unstructured):
    out = convert(client, unstructured)

    assert out["diagnostics"], "expected the heading fix to be reported"
    for d in out["diagnostics"]:
        assert set(d) >= {"code", "message", "severity", "retryable"}
        assert isinstance(d["retryable"], bool)


def test_info_diagnostics_stay_out_of_legacy_warnings(client, unstructured):
    """v1 `warnings` means "something went wrong", not "we tidied the headings"."""
    out = convert(client, unstructured)

    assert any(d["code"] == "heading_normalized" for d in out["diagnostics"])
    assert not any("heading_normalized" in w for w in out["warnings"])


def test_headingless_document_still_satisfies_the_invariants(client, unstructured):
    sections = convert(client, unstructured)["sections"]

    assert [s["level"] for s in sections] == [1, 2]


def test_title_only_document_reports_that_it_could_not_be_structured(
    client, tmp_path
):
    """We cannot invent a section body. Say so instead of shipping a bad doc."""
    p = tmp_path / "제목뿐.md"
    p.write_text("제목뿐인 문서.\n", encoding="utf-8")
    out = convert(client, p)

    codes = {d["code"] for d in out["diagnostics"]}
    assert "structure_incomplete" in codes
    assert any(w.startswith("structure_incomplete") for w in out["warnings"])


def test_missing_source_returns_a_structured_retryable_flag(client, tmp_path):
    r = client.post(
        "/v2/convert",
        json={"source": {"kind": "path", "path": str(tmp_path / "없음.pdf")}},
    )
    assert r.status_code == 404
    assert r.json()["code"] == "SOURCE_NOT_FOUND"
    assert r.json()["retryable"] is False


# --------------------------------------------------------------------------
# budget
# --------------------------------------------------------------------------


def test_small_document_routes_to_processed(client, source):
    b = convert(client, source)["budget"]

    assert b["routing"] == "processed"
    assert b["reason"] is None
    assert 0 < b["est_tokens"] < b["budget_tokens"]


def test_over_budget_document_routes_to_excepted_but_still_converts(client, source):
    out = convert(client, source, budget_tokens=10)

    assert out["budget"]["routing"] == "excepted"
    assert out["budget"]["reason"] == "token_budget_exceeded"
    assert out["body"].strip(), "an excepted document is still converted in full"
    assert any(d["code"] == "token_budget_exceeded" for d in out["diagnostics"])


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------


def test_bytes_source_needs_no_shared_filesystem(client, source):
    import base64

    r = client.post(
        "/v2/convert",
        json={
            "source": {
                "kind": "bytes",
                "filename": "업로드.md",
                "content_base64": base64.b64encode(source.read_bytes()).decode(),
            }
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["body"].strip()
    assert r.json()["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_unfetchable_uri_scheme_is_rejected(client):
    r = client.post(
        "/v2/convert",
        json={
            "source": {
                "kind": "uri",
                "filename": "x.pdf",
                "uri": "http://169.254.169.254/latest/meta-data/",
            }
        },
    )
    assert r.status_code == 400
    assert r.json()["code"] == "UNSUPPORTED_SOURCE"


# --------------------------------------------------------------------------
# capabilities
# --------------------------------------------------------------------------


def test_capabilities_report_no_write_back_anywhere(client):
    caps = client.get("/capabilities").json()

    assert caps["write_back_supported"] is False
    assert caps["formats"], "expected a non-empty capability table"
    for f in caps["formats"]:
        assert f["parse"] is True
        assert f["write_back"] is False, f"{f['format']} must not claim write-back"


def test_capabilities_separate_ocr_and_page_mapping_per_format(client):
    by_format = {f["format"]: f for f in client.get("/capabilities").json()["formats"]}

    assert by_format["hwpx"]["page_mapping"] is False
    assert by_format["pptx"]["page_mapping"] is True
    assert by_format["hwp"]["ocr"] is False
    assert by_format["pdf"]["ocr"] is True


# --------------------------------------------------------------------------
# stale-source rejection (issue #2: "expected source SHA와 다르면 거부")
# --------------------------------------------------------------------------


def test_matching_expected_sha_converts(client, source):
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    out = convert(client, source, expected_source_sha256=sha)

    assert out["source_sha256"] == sha


def test_mismatched_expected_sha_is_refused_as_stale(client, source):
    r = client.post(
        "/v2/convert",
        json={
            "source": {"kind": "path", "path": str(source)},
            "expected_source_sha256": "0" * 64,
        },
    )
    assert r.status_code == 409
    assert r.json()["code"] == "SOURCE_HASH_MISMATCH"
    assert r.json()["retryable"] is False


def test_source_edited_after_the_caller_read_it_is_refused(client, source):
    """The race the check exists for."""
    stale = hashlib.sha256(source.read_bytes()).hexdigest()
    source.write_text("# 제목\n\n## 절\n\n누군가 방금 고친 내용.\n", encoding="utf-8")

    r = client.post(
        "/v2/convert",
        json={
            "source": {"kind": "path", "path": str(source)},
            "expected_source_sha256": stale,
        },
    )
    assert r.status_code == 409


# --------------------------------------------------------------------------
# page markers
# --------------------------------------------------------------------------


def test_page_markers_map_back_to_source_page(client, tmp_path):
    """converters.page_marker writes them; structure.py reads them back."""
    from app.converters import page_marker

    p = tmp_path / "페이지문서.md"
    p.write_text(
        f"{page_marker(1)}\n\n# 문서\n\n## 첫 장\n\n1쪽 본문.\n\n"
        f"{page_marker(2)}\n\n## 둘째 장\n\n2쪽 본문.\n\n"
        f"{page_marker(7)}\n\n## 일곱째 장\n\n7쪽 본문.\n",
        encoding="utf-8",
    )
    sections = convert(client, p)["sections"]

    by_heading = {s["heading"]: s["source_page"] for s in sections}
    assert by_heading["첫 장"] == 1
    assert by_heading["둘째 장"] == 2
    assert by_heading["일곱째 장"] == 7


def test_page_markers_do_not_leak_into_the_title_or_language(client, tmp_path):
    from app.converters import page_marker

    p = tmp_path / "표지.md"
    p.write_text(
        f"{page_marker(1)}\n\n입찰 공고문\n\n본문 내용입니다.\n", encoding="utf-8"
    )
    fm = convert(client, p)["frontmatter"]

    assert "page" not in fm["title"]
    assert fm["title"] == "입찰 공고문"
    assert fm["language"] == "ko"


# --------------------------------------------------------------------------
# frontmatter carries the canonical hash and converter version
# --------------------------------------------------------------------------


def test_frontmatter_carries_canonical_sha_and_converter_version(client, source):
    out = convert(client, source)
    fm = out["frontmatter"]

    assert fm["schema_version"] == "1.1.0"
    assert fm["canonical_sha256"] == out["canonical_sha256"]
    assert fm["converter_version"] == out["converter"]["version"]


def test_chunk_frontmatter_points_at_the_parent_document_version(client, source):
    out = convert(client, source, chunking={"enabled": True})

    assert out["chunks"]
    for chunk in out["chunks"]:
        assert chunk["frontmatter"]["canonical_sha256"] == out["canonical_sha256"]
