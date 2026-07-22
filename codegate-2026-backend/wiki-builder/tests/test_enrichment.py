from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import wiki_builder.enrichment as enrichment_module
from wiki_builder.config import WikiConfig, WikiScope, scoped_config
from wiki_builder.corpus import extract_links
from wiki_builder.enrichment import (
    _enforce_external_processing_policy,
    enrich_documents,
    sanitize_optional_grounding,
    validate_grounding,
)
from wiki_builder.errors import ValidationError
from wiki_builder.markdown import load_documents


def _valid_record(config: WikiConfig):
    document = next(doc for doc in load_documents(config) if doc.doc_id == "REG-000001")
    links = [
        link
        for link in extract_links([*load_documents(config)])
        if link.from_doc_id == document.doc_id
    ]
    first, second, third = document.sections
    link = links[0]
    record = {
        "doc_id": document.doc_id,
        "summary": {
            "text": "정보자산 보호의 기본 원칙과 책임을 정한다. 적용 대상과 신고 원칙도 규정한다.",
            "evidence": [
                {
                    "section_id": first.section_id,
                    "quote": (
                        "이 규정은 조직의 정보자산을 안전하게 보호하고 정보보호 업무의 "
                        "기본 원칙과 책임을 정하는 것을 목적으로 한다."
                    ),
                }
            ],
        },
        "key_points": [
            {
                "text": "모든 사용자에게 적용한다.",
                "evidence": [
                    {
                        "section_id": second.section_id,
                        "quote": (
                            "이 규정은 임직원, 계약직, 외부 협력사 및 조직의 정보시스템을 "
                            "사용하는 모든 사람에게 적용한다."
                        ),
                    }
                ],
            },
            {
                "text": "최소한의 정보에만 접근한다.",
                "evidence": [
                    {
                        "section_id": third.section_id,
                        "quote": "업무상 필요한 최소한의 정보에만 접근한다.",
                    }
                ],
            },
            {
                "text": "보안사고가 의심되면 신고한다.",
                "evidence": [
                    {
                        "section_id": third.section_id,
                        "quote": "보안사고가 의심되면 즉시 신고한다.",
                    }
                ],
            },
        ],
        "keywords": ["정보보호", "정보자산", "임직원", "최소한", "보안사고"],
        "entities": [
            {
                "name": "정보보호위원회",
                "type": "organization",
                "evidence": [
                    {
                        "section_id": first.section_id,
                        "quote": "정보보호 업무의 기본 원칙과 책임",
                    }
                ],
            }
        ],
        "faq": [
            {
                "question": "누구에게 적용되는가?",
                "answer": "조직의 정보시스템을 사용하는 모든 사람에게 적용한다.",
                "evidence": [
                    {
                        "section_id": second.section_id,
                        "quote": (
                            "이 규정은 임직원, 계약직, 외부 협력사 및 조직의 정보시스템을 "
                            "사용하는 모든 사람에게 적용한다."
                        ),
                    }
                ],
            },
            {
                "question": "사고가 의심되면 어떻게 하는가?",
                "answer": "즉시 신고한다.",
                "evidence": [
                    {
                        "section_id": third.section_id,
                        "quote": "보안사고가 의심되면 즉시 신고한다.",
                    }
                ],
            },
        ],
        "relations": [
            {
                "to_doc_id": link.to_doc_id,
                "relation_type": "references",
                "evidence": [
                    {
                        "section_id": link.from_section_id,
                        "quote": link.evidence_quote,
                    }
                ],
            }
        ],
    }
    return document, links, record


def test_grounded_enrichment_is_accepted(config: WikiConfig) -> None:
    document, links, record = _valid_record(config)
    validate_grounding(config, document, links, record)


def test_nonexistent_quote_is_rejected(config: WikiConfig) -> None:
    document, links, record = _valid_record(config)
    invalid = deepcopy(record)
    invalid["faq"][0]["evidence"][0]["quote"] = "원문에 없는 문장"
    with pytest.raises(ValidationError, match="정확히 존재하지 않는"):
        validate_grounding(config, document, links, invalid)


def test_hallucinated_number_is_rejected(config: WikiConfig) -> None:
    document, links, record = _valid_record(config)
    invalid = deepcopy(record)
    invalid["faq"][0]["answer"] = "9999일 안에 처리한다."
    with pytest.raises(ValidationError, match="원문에 없는 숫자"):
        validate_grounding(config, document, links, invalid)


def test_undeclared_relation_target_is_rejected(config: WikiConfig) -> None:
    document, links, record = _valid_record(config)
    invalid = deepcopy(record)
    invalid["relations"][0]["to_doc_id"] = "REG-000003"
    with pytest.raises(ValidationError, match="링크 후보"):
        validate_grounding(config, document, links, invalid)


def test_relation_exact_subquote_with_markdown_link_is_accepted(
    config: WikiConfig,
) -> None:
    document, links, record = _valid_record(config)
    link = links[0]
    link_start = link.evidence_quote.index("[")
    record["relations"][0]["evidence"][0]["quote"] = link.evidence_quote[link_start:]
    validate_grounding(config, document, links, record)


def test_relation_subquote_without_markdown_link_is_rejected(
    config: WikiConfig,
) -> None:
    document, links, record = _valid_record(config)
    link = links[0]
    quote = link.evidence_quote.split("[", maxsplit=1)[0].strip()
    assert quote
    record["relations"][0]["evidence"][0]["quote"] = quote
    with pytest.raises(ValidationError, match="명시적 링크"):
        validate_grounding(config, document, links, record)


def test_optional_grounding_sanitizer_drops_unsupported_suggestions(
    config: WikiConfig,
) -> None:
    document, _, record = _valid_record(config)
    invalid_entity = deepcopy(record["entities"][0])
    invalid_entity["evidence"][0]["quote"] = document.title
    record["entities"].append(invalid_entity)
    record["keywords"].append("원문에 없는 추천 키워드")

    cleaned = sanitize_optional_grounding(document, record)

    assert cleaned["keywords"] == record["keywords"][:-1]
    assert cleaned["entities"] == record["entities"][:1]
    assert record["keywords"][-1] == "원문에 없는 추천 키워드"


def test_production_nonpublic_documents_require_paid_data_policy(
    config: WikiConfig,
    tmp_path: Path,
) -> None:
    scope = WikiScope(
        tenant_id="tenant-a",
        wiki_id="private-wiki",
        input_dir=config.resolve_path("input_dir"),
        storage_root=tmp_path,
        synthetic_corpus=False,
    )
    active_config = scoped_config(config, scope)
    with pytest.raises(ValidationError, match="paid-no-training"):
        _enforce_external_processing_policy(
            active_config,
            load_documents(active_config),
        )


def test_synthetic_corpus_can_use_development_policy(
    config: WikiConfig,
    tmp_path: Path,
) -> None:
    scope = WikiScope(
        tenant_id="tenant-a",
        wiki_id="sample",
        input_dir=config.resolve_path("input_dir"),
        storage_root=tmp_path,
        synthetic_corpus=True,
    )
    active_config = scoped_config(config, scope)
    _enforce_external_processing_policy(active_config, load_documents(active_config))


def test_restricted_document_is_always_blocked(
    config: WikiConfig,
    tmp_path: Path,
) -> None:
    scope = WikiScope(
        tenant_id="tenant-a",
        wiki_id="restricted-wiki",
        input_dir=config.resolve_path("input_dir"),
        storage_root=tmp_path,
        synthetic_corpus=False,
    )
    active_config = scoped_config(config, scope)
    raw = deepcopy(active_config.raw)
    raw["provider"]["data_policy"] = "paid-no-training"
    paid_config = WikiConfig(path=active_config.path, raw=raw)
    document = load_documents(paid_config)[0]
    restricted = replace(
        document,
        metadata={**document.metadata, "access": "restricted"},
    )
    with pytest.raises(ValidationError, match="restricted"):
        _enforce_external_processing_policy(paid_config, [restricted])


def test_failed_refresh_keeps_previous_approved_record_for_undo_recovery(
    config: WikiConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_id = "MAN-000003"
    scope = WikiScope(
        tenant_id="tenant-a",
        wiki_id="recovery-wiki",
        input_dir=config.resolve_path("input_dir"),
        storage_root=tmp_path,
        synthetic_corpus=True,
    )
    previous_path = config.resolve_path("enrichment_dir") / "approved" / f"{document_id}.json"
    approved_path = scope.enrichment_dir / "approved" / f"{document_id}.json"
    approved_path.parent.mkdir(parents=True)
    previous_bytes = previous_path.read_bytes()
    approved_path.write_bytes(previous_bytes)

    class InvalidGeminiClient:
        def __init__(self, _config: WikiConfig) -> None:
            pass

        def __enter__(self) -> InvalidGeminiClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def complete(self, *_args: object) -> object:
            return SimpleNamespace(text="{}", usage={})

    monkeypatch.setattr(enrichment_module, "get_api_key", lambda *_args, **_kwargs: "key")
    monkeypatch.setattr(enrichment_module, "GeminiClient", InvalidGeminiClient)

    run = enrich_documents(
        config,
        selected_ids={document_id},
        refresh=True,
        scope=scope,
    )

    assert run.failed == [document_id]
    assert approved_path.read_bytes() == previous_bytes
    assert (scope.enrichment_dir / "needs-review" / f"{document_id}.json").is_file()
