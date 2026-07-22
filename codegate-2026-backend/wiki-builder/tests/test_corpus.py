from __future__ import annotations

import json
from collections import Counter

from wiki_builder.builder import prepare_build_data
from wiki_builder.config import WikiConfig
from wiki_builder.corpus import split_section_text, verify_expected_corpus


def test_corpus_acceptance_counts(
    config: WikiConfig,
    sample_expectations: dict,
) -> None:
    data = prepare_build_data(config)
    verify_expected_corpus(sample_expectations, data)
    assert len(data.documents) == 50
    assert len(data.chunks) == 148
    assert len(data.aliases["entries"]) == 100
    assert len(data.links) == 38
    assert Counter(doc.metadata["doc_type"] for doc in data.documents) == {
        "regulation": 20,
        "manual": 20,
        "report": 10,
    }
    assert Counter(doc.metadata["status"] for doc in data.documents) == {
        "active": 44,
        "archived": 2,
        "draft": 3,
        "superseded": 1,
    }


def test_build_data_is_deterministic(config: WikiConfig) -> None:
    first = prepare_build_data(config)
    second = prepare_build_data(config)
    for attribute in ("manifest", "chunks", "aliases", "links", "enrichments"):
        left = json.dumps(getattr(first, attribute), ensure_ascii=False, sort_keys=True)
        right = json.dumps(getattr(second, attribute), ensure_ascii=False, sort_keys=True)
        assert left == right


def test_chunk_ids_and_source_only_embedding_text(config: WikiConfig) -> None:
    data = prepare_build_data(config)
    first = next(chunk for chunk in data.chunks if chunk["doc_id"] == "REG-000001")
    assert first["chunk_id"].startswith("REG-000001@3#sec-")
    assert first["chunk_id"].endswith(":c001")
    assert first["embedding_text"].startswith("정보보호 기본 규정\n제1조 목적\n")
    assert "FAQ" not in first["embedding_text"]


def test_future_long_section_splits_with_overlap() -> None:
    text = "가" * 80 + "\n\n" + "나" * 80 + "\n\n" + "다" * 80
    parts = split_section_text(text, max_chars=120, overlap_chars=20)
    assert len(parts) >= 2
    assert all(0 < len(part) <= 120 for part in parts)


def test_every_link_target_exists(config: WikiConfig) -> None:
    data = prepare_build_data(config)
    ids = {document.doc_id for document in data.documents}
    assert all(link["to_doc_id"] in ids for link in data.links)
    assert all(
        link["origin"]
        == ("explicit" if link["relation_type"] == "references" else "llm-classified")
        for link in data.links
    )
