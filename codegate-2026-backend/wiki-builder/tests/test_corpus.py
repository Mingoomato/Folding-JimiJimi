from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

from wiki_builder.builder import prepare_build_data
from wiki_builder.config import WikiConfig
from wiki_builder.corpus import discover_links, split_section_text, verify_expected_corpus
from wiki_builder.models import Document, Section


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


def test_similar_production_markdown_creates_grounded_semantic_links(config: WikiConfig) -> None:
    raw = deepcopy(config.raw)
    raw["scope"] = {"synthetic_corpus": False}
    production = WikiConfig(path=config.path, raw=raw)

    def document(doc_id: str, body: str) -> Document:
        section = Section(
            section_id=f"sec-{doc_id[-10:].lower():0>10}",
            heading="업무 현황",
            heading_path=("업무 현황",),
            ordinal=1,
            text=body,
        )
        return Document(
            input_path=Path(f"{doc_id}.md"),
            input_relative_path=f"{doc_id}.md",
            output_relative_path=f"docs/{doc_id}.md",
            metadata={"id": doc_id, "revision": "1", "title": doc_id},
            original_sha256="a" * 64,
            normalized_markdown=body,
            normalized_body=body,
            sections=(section,),
            h1_title=doc_id,
        )

    shared = "구독 이탈률 상승 원인 분석 배송 지연 클레임 물류팀 협의 인터뷰 일정"
    documents = [
        document("DOC-REPORT-A", shared + " 주간 성과 집계"),
        document("DOC-REPORT-B", shared + " 일일 진행 기록"),
        document("DOC-UNRELATED", "사내 보안 규정 비밀번호 접근 권한 암호화 정책"),
    ]

    links = discover_links(production, documents)
    pairs = {(link.from_doc_id, link.to_doc_id) for link in links}
    assert ("DOC-REPORT-A", "DOC-REPORT-B") in pairs
    assert ("DOC-REPORT-B", "DOC-REPORT-A") in pairs
    assert all("DOC-UNRELATED" not in pair for pair in pairs)
    assert all(link.origin == "semantic" for link in links)
    source_text = documents[0].normalized_body + documents[1].normalized_body
    assert all(link.evidence_quote in source_text for link in links)
