from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from wiki_builder import service as service_module
from wiki_builder.builder import build_wiki
from wiki_builder.config import WikiConfig, WikiScope
from wiki_builder.enrichment import EnrichmentRun
from wiki_builder.errors import UnsafeOutputError, ValidationError
from wiki_builder.service import WikiBuildRequest, WikiBuildService

GENERAL_DOCUMENT = """---
schema_version: "1.0.0"
id: DOC-000001
title: 서비스 운영 메모
doc_type: general
language: ko
revision: "1"
status: active
official_number: null
authority_level: reference
issuing_org: 운영팀
issued_on: 2026-07-21
effective_from: null
effective_to: null
source:
  filename: service-note.txt
  uri: source://uploads/service-note.txt
  sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  verification: verified
access: internal
tags: []
aliases: []
---

# 서비스 운영 메모

## 목적

서비스 운영 상태를 기록한다.
"""


def _scope(config: WikiConfig, tmp_path: Path) -> WikiScope:
    input_dir = tmp_path / "source-md"
    shutil.copytree(config.resolve_path("input_dir"), input_dir)
    return WikiScope(
        tenant_id="tenant-a",
        wiki_id="operations",
        input_dir=input_dir,
        storage_root=tmp_path / "storage",
        synthetic_corpus=True,
        actor_id="tester",
    )


def _request(scope: WikiScope, expected_current_build_id: str | None) -> WikiBuildRequest:
    return WikiBuildRequest(
        tenant_id=scope.tenant_id,
        wiki_id=scope.wiki_id,
        input_dir=scope.input_dir,
        storage_root=scope.storage_root,
        actor_id=scope.actor_id,
        synthetic_corpus=scope.synthetic_corpus,
        expected_current_build_id=expected_current_build_id,
    )


def test_add_update_delete_creates_distinct_immutable_builds(
    config: WikiConfig,
    tmp_path: Path,
) -> None:
    scope = _scope(config, tmp_path)
    first = build_wiki(config, scope, activate_incomplete=True)
    assert len(first.documents) == 50

    added_path = scope.input_dir / "DOC-000001.md"
    added_path.write_text(GENERAL_DOCUMENT, encoding="utf-8")
    second = build_wiki(config, scope, activate_incomplete=True)
    assert len(second.documents) == 51
    assert second.build_id != first.build_id
    assert first.build_dir.is_dir()
    assert (second.build_dir / "docs" / "general" / "DOC-000001.md").is_file()

    added_path.write_text(
        GENERAL_DOCUMENT.replace("운영 상태를 기록한다.", "운영 상태와 변경 이력을 기록한다."),
        encoding="utf-8",
    )
    third = build_wiki(config, scope, activate_incomplete=True)
    assert third.build_id not in {first.build_id, second.build_id}

    added_path.unlink()
    fourth = build_wiki(config, scope, activate_incomplete=True)
    assert len(fourth.documents) == 50
    assert fourth.build_id != third.build_id
    assert first.build_dir.is_dir() and second.build_dir.is_dir() and third.build_dir.is_dir()
    current = json.loads(scope.current_pointer.read_text(encoding="utf-8"))
    assert current["build_id"] == fourth.build_id


def test_same_inputs_reuse_same_immutable_build(config: WikiConfig, tmp_path: Path) -> None:
    scope = _scope(config, tmp_path)
    first = build_wiki(config, scope, activate_incomplete=True)
    second = build_wiki(config, scope, activate_incomplete=True)
    assert second.build_id == first.build_id
    assert second.created is False


def test_incomplete_build_is_not_activated_by_default(
    config: WikiConfig,
    tmp_path: Path,
) -> None:
    scope = _scope(config, tmp_path)
    result = build_wiki(config, scope)
    assert result.activated is False
    assert not scope.current_pointer.exists()


def test_scope_rejects_path_traversal(config: WikiConfig, tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="tenant_id"):
        WikiScope(
            tenant_id="../other",
            wiki_id="wiki",
            input_dir=config.resolve_path("input_dir"),
            storage_root=tmp_path,
        )


def test_stale_activation_is_rejected(config: WikiConfig, tmp_path: Path) -> None:
    scope = _scope(config, tmp_path)
    first = build_wiki(config, scope, activate_incomplete=True)

    added_path = scope.input_dir / "DOC-000001.md"
    added_path.write_text(GENERAL_DOCUMENT, encoding="utf-8")
    second = build_wiki(
        config,
        scope,
        activate_incomplete=True,
        expected_current_build_id=first.build_id,
    )

    added_path.write_text(
        GENERAL_DOCUMENT.replace("운영 상태를 기록한다.", "오래된 요청의 변경이다."),
        encoding="utf-8",
    )
    with pytest.raises(UnsafeOutputError, match="요청 시작 시점과 달라"):
        build_wiki(
            config,
            scope,
            activate_incomplete=True,
            expected_current_build_id=first.build_id,
        )
    current = json.loads(scope.current_pointer.read_text(encoding="utf-8"))
    assert current["build_id"] == second.build_id


def test_service_stages_validates_and_activates_candidate(
    config: WikiConfig,
    tmp_path: Path,
) -> None:
    scope = _scope(config, tmp_path)
    service = WikiBuildService(config)
    request = _request(scope, expected_current_build_id=None)

    candidate = service.stage(request)

    assert candidate.activated is False
    assert not scope.current_pointer.exists()
    assert not (scope.wiki_root / "audit").exists()
    validation = service.validate_candidate(
        request,
        candidate.build_id,
        require_enrichment=False,
        check_reproducible=False,
    )
    assert validation["build_id"] == candidate.build_id

    activated = service.activate_candidate(
        request,
        candidate.build_id,
        activate_incomplete=True,
        check_reproducible=False,
    )

    assert activated["activated"] is True
    current = json.loads(scope.current_pointer.read_text(encoding="utf-8"))
    assert current["build_id"] == candidate.build_id
    assert len(list((scope.wiki_root / "audit" / "builds").glob("*.json"))) == 1


def test_service_rejects_invalid_candidate_before_activation(
    config: WikiConfig,
    tmp_path: Path,
) -> None:
    scope = _scope(config, tmp_path)
    service = WikiBuildService(config)
    request = _request(scope, expected_current_build_id=None)
    candidate = service.stage(request)
    manifest_path = candidate.build_dir / "manifest.jsonl"
    manifest_path.write_text(manifest_path.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        service.activate_candidate(
            request,
            candidate.build_id,
            activate_incomplete=True,
            check_reproducible=False,
        )

    assert not scope.current_pointer.exists()
    assert not (scope.wiki_root / "audit").exists()


def test_service_candidate_activation_requires_valid_id_and_cas(
    config: WikiConfig,
    tmp_path: Path,
) -> None:
    scope = _scope(config, tmp_path)
    service = WikiBuildService(config)
    first = build_wiki(config, scope, activate_incomplete=True)

    with pytest.raises(ValidationError, match="build_id"):
        service.validate_candidate(
            _request(scope, expected_current_build_id=first.build_id),
            "../build-0123456789abcdefabcd",
            require_enrichment=False,
            check_reproducible=False,
        )

    added_path = scope.input_dir / "DOC-000001.md"
    added_path.write_text(GENERAL_DOCUMENT, encoding="utf-8")
    stale_request = _request(scope, expected_current_build_id="build-00000000000000000000")
    candidate = service.stage(stale_request)

    with pytest.raises(UnsafeOutputError, match="요청 시작 시점과 달라"):
        service.activate_candidate(
            stale_request,
            candidate.build_id,
            activate_incomplete=True,
            check_reproducible=False,
        )

    current = json.loads(scope.current_pointer.read_text(encoding="utf-8"))
    assert current["build_id"] == first.build_id


def test_configured_enrichment_is_a_noop_without_api_key(
    config: WikiConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope(config, tmp_path)
    service = WikiBuildService(config)
    request = _request(scope, expected_current_build_id=None)
    monkeypatch.setattr(service_module, "get_api_key", lambda _config, required=False: None)

    def unexpected_enrichment(*args, **kwargs):
        raise AssertionError("missing API key must not invoke enrichment")

    monkeypatch.setattr(service_module, "enrich_documents", unexpected_enrichment)

    assert service.enrich_documents_if_configured(request, refresh=False) is None


def test_configured_enrichment_delegates_cache_and_policy_checks(
    config: WikiConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope(config, tmp_path)
    service = WikiBuildService(config)
    request = _request(scope, expected_current_build_id=None)
    expected = EnrichmentRun(skipped=["REG-000001"])
    captured: dict[str, object] = {}
    monkeypatch.setattr(service_module, "get_api_key", lambda _config, required=False: "key")

    def fake_enrichment(active_config, **kwargs):
        captured["config"] = active_config
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(service_module, "enrich_documents", fake_enrichment)

    result = service.enrich_documents_if_configured(
        request,
        doc_ids={"REG-000001"},
        refresh=False,
    )

    assert result is expected
    assert captured == {
        "config": config,
        "selected_ids": {"REG-000001"},
        "refresh": False,
        "scope": scope,
    }


def test_configured_enrichment_refuses_failed_records(
    config: WikiConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope(config, tmp_path)
    service = WikiBuildService(config)
    request = _request(scope, expected_current_build_id=None)
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    monkeypatch.setattr(service_module, "get_api_key", lambda _config, required=False: "key")
    monkeypatch.setattr(
        service_module,
        "enrich_documents",
        lambda *_args, **_kwargs: EnrichmentRun(failed=["REG-000001"]),
    )

    with pytest.raises(ValidationError, match="REG-000001"):
        service.enrich_documents_if_configured(request, refresh=False)


def test_configured_enrichment_reuses_scoped_cache_and_stages_complete_build(
    config: WikiConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope(config, tmp_path)
    approved_source = config.resolve_path("enrichment_dir") / "approved"
    approved_target = scope.enrichment_dir / "approved"
    approved_target.parent.mkdir(parents=True)
    shutil.copytree(approved_source, approved_target)
    service = WikiBuildService(config)
    request = _request(scope, expected_current_build_id=None)
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    monkeypatch.setattr(service_module, "get_api_key", lambda _config, required=False: "key")

    run = service.enrich_documents_if_configured(request, refresh=False)
    candidate = service.stage(request)

    assert run is not None
    assert len(run.skipped) == 50
    assert run.processed == []
    assert run.failed == []
    build_meta = json.loads((candidate.build_dir / "build-meta.json").read_text(encoding="utf-8"))
    assert build_meta["complete"] is True
    assert candidate.activated is False
    assert (candidate.build_dir / "retrieval" / "links.jsonl").is_file()
