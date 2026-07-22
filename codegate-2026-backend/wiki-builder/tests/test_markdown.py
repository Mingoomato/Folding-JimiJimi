from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest

from wiki_builder.builder import prepare_build_data
from wiki_builder.config import WikiConfig
from wiki_builder.errors import ValidationError
from wiki_builder.markdown import (
    load_documents,
    normalize_text,
    split_front_matter,
    strip_generated_anchors,
)


def _semantic_fragment(chunk_no: str, body: str, *, title: str = "분할 문서") -> str:
    return f"""---
schema_version: "1.0.0"
id: DOC-CHUNKED
title: {title}
doc_type: general
language: ko
revision: "1"
status: active
chunk_no: {chunk_no}
official_number: null
authority_level: null
issuing_org: null
issued_on: null
effective_from: null
effective_to: null
source:
  filename: chunked.pdf
  uri: source://chunked.pdf
  sha256: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
access: internal
tags: []
aliases: []
---

{body}
"""


def _semantic_config(config: WikiConfig, input_dir) -> WikiConfig:
    raw = deepcopy(config.raw)
    raw["paths"]["input_dir"] = str(input_dir)
    return WikiConfig(path=config.path, raw=raw)


def test_loads_expected_documents_without_modifying_inputs(config: WikiConfig) -> None:
    input_dir = config.resolve_path("input_dir")
    before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(input_dir.glob("*.md"))
    }
    documents = load_documents(config)
    after = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(input_dir.glob("*.md"))
    }
    assert len(documents) == 50
    assert sum(len(document.sections) for document in documents) == 148
    assert before == after


def test_generated_anchors_do_not_change_body(config: WikiConfig) -> None:
    for document in load_documents(config):
        source = normalize_text(document.input_path.read_text(encoding="utf-8"))
        _, source_body = split_front_matter(source, document.input_path)
        assert strip_generated_anchors(document.normalized_body) == normalize_text(source_body)
        assert document.normalized_body.count('<a id="sec-') == len(document.sections)


def test_front_matter_marks_unavailable_source_verification(config: WikiConfig) -> None:
    document = next(doc for doc in load_documents(config) if doc.doc_id == "REG-000001")
    assert document.metadata["source"]["verification"] == "unavailable"
    assert document.metadata["source"]["sha256"].endswith("0001")
    assert '<a id="art-1"></a>' in document.normalized_body


def test_document_preamble_is_retained_in_first_section(config: WikiConfig) -> None:
    document = next(doc for doc in load_documents(config) if doc.doc_id == "REG-000018")
    assert "효력이 종료되었으며" in document.sections[0].text
    assert "REG-000019.md" in document.sections[0].text


def test_document_count_limit_is_enforced(config: WikiConfig) -> None:
    raw = deepcopy(config.raw)
    raw["input_limits"]["max_documents"] = 49
    limited = WikiConfig(path=config.path, raw=raw)
    with pytest.raises(ValidationError, match="문서 수가 제한"):
        load_documents(limited)


def test_symlink_input_is_rejected(config: WikiConfig, tmp_path) -> None:
    input_dir = tmp_path / "source-md"
    input_dir.mkdir()
    source = config.resolve_path("input_dir") / "REG-000001.md"
    (input_dir / source.name).symlink_to(source)
    raw = deepcopy(config.raw)
    raw["paths"]["input_dir"] = str(input_dir)
    linked = WikiConfig(path=config.path, raw=raw)
    with pytest.raises(ValidationError, match="심볼릭 링크 입력"):
        load_documents(linked)


def test_source_fragments_form_one_logical_document_without_rechunking(
    config: WikiConfig,
    tmp_path,
) -> None:
    input_dir = tmp_path / "source-md"
    input_dir.mkdir()
    long_semantic_unit = "가" * 2600
    first = input_dir / "chunk-001.md"
    second = input_dir / "chunk-002.md"
    first.write_text(
        _semantic_fragment("chunk-001", f"# 분할 문서\n\n{long_semantic_unit}"),
        encoding="utf-8",
    )
    second.write_text(
        _semantic_fragment(
            "chunk-002",
            "<!-- section: 후속 의미 단위 -->\n\n두 번째 의미 단위다.",
        ),
        encoding="utf-8",
    )
    before = {path.name: path.read_bytes() for path in (first, second)}
    fragmented = _semantic_config(config, input_dir)

    documents = load_documents(fragmented)
    data = prepare_build_data(fragmented)

    assert len(documents) == 1
    document = documents[0]
    assert document.doc_id == "DOC-CHUNKED"
    assert document.chunking_mode == "source-fragments"
    assert [item.chunk_no for item in document.source_fragments] == [
        "chunk-001",
        "chunk-002",
    ]
    assert len(document.sections) == 2
    assert len({section.section_id for section in document.sections}) == 2
    assert document.metadata["authority_level"] is None
    assert document.metadata["issuing_org"] is None
    assert document.normalized_body.splitlines().count("# 분할 문서") == 1
    assert document.normalized_body.count("<!-- source-chunk:") == 2
    assert document.normalized_body.count("\n## ") == 2
    assert "## 후속 의미 단위 · chunk-002" in document.normalized_body
    assert len(data.chunks) == 2
    assert data.chunks[0]["chunk_type"] == "source-fragment"
    assert data.chunks[0]["source_chunk_no"] == "chunk-001"
    assert data.chunks[0]["char_count"] > int(config.raw["chunking"]["max_chars"])
    assert data.chunks[1]["source_input_path"] == "source-md/chunk-002.md"
    manifest = data.manifest[0]
    assert manifest["source_fragment_count"] == 2
    assert manifest["chunking_mode"] == "source-fragments"
    assert [item["chunk_no"] for item in manifest["ingest"]["fragments"]] == [
        "chunk-001",
        "chunk-002",
    ]
    assert before == {path.name: path.read_bytes() for path in (first, second)}


def test_source_fragment_metadata_must_match(config: WikiConfig, tmp_path) -> None:
    input_dir = tmp_path / "source-md"
    input_dir.mkdir()
    (input_dir / "chunk-001.md").write_text(
        _semantic_fragment("chunk-001", "# 분할 문서\n\n첫 번째다."),
        encoding="utf-8",
    )
    (input_dir / "chunk-002.md").write_text(
        _semantic_fragment("chunk-002", "두 번째다.", title="다른 제목"),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="front matter가 일치하지 않습니다"):
        load_documents(_semantic_config(config, input_dir))


def test_source_fragment_h1_must_be_first_content_line(
    config: WikiConfig,
    tmp_path,
) -> None:
    input_dir = tmp_path / "source-md"
    input_dir.mkdir()
    (input_dir / "chunk-001.md").write_text(
        _semantic_fragment("chunk-001", "앞선 본문\n\n# 분할 문서\n\n후속 본문"),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="H1은 본문 첫 줄"):
        load_documents(_semantic_config(config, input_dir))
