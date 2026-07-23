from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from codegate_api.knowledge.llmwiki import (
    InProcessLLMWikiBuildRunner,
    LLMWikiCompatibilityError,
    _convert_links,
)


def _runner(tmp_path: Path) -> InProcessLLMWikiBuildRunner:
    integrated_root = Path(__file__).resolve().parents[2]
    return InProcessLLMWikiBuildRunner(
        project_root=integrated_root / "codegate-2026-backend",
        input_root=tmp_path / "input",
        storage_root=tmp_path / "storage",
        tenant_id="local",
        wiki_id="workspace",
        actor_id="test-agent",
    )


def test_semantic_link_uses_first_canonical_chunk_when_quote_repeats() -> None:
    chunks = {
        ("DOC-SOURCE", "sec-shared"): [
            {"chunk_id": "chunk-1", "text": "same evidence"},
            {"chunk_id": "chunk-2", "text": "same evidence"},
        ]
    }
    links = _convert_links(
        [
            {
                "from_doc_id": "DOC-SOURCE",
                "from_section_id": "sec-shared",
                "to_doc_id": "DOC-TARGET",
                "evidence_quote": "section heading not repeated in chunk text",
                "relation_type": "references",
                "origin": "semantic",
            }
        ],
        chunks,
    )

    assert links[0]["evidence_chunk_ids"] == ["chunk-1"]
    assert links[0]["status"] == "VERIFIED"


def test_runner_enriches_before_staging_without_activating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner(tmp_path)
    calls: list[object] = []

    class FakeRequest:
        def __init__(self, **values: object) -> None:
            self.values = values

    class FakeService:
        def __init__(self, config: object) -> None:
            calls.append(("service", config))

        def enrich_documents_if_configured(
            self,
            request: FakeRequest,
            *,
            refresh: bool,
        ) -> None:
            calls.append(("enrich", request.values, refresh))

        def stage(self, request: FakeRequest) -> object:
            calls.append(("stage", request.values))
            return SimpleNamespace(activated=False, build_id="build-0123456789abcdefabcd")

    config = object()
    config_module = SimpleNamespace(load_config=lambda _path: config)
    service_module = SimpleNamespace(WikiBuildRequest=FakeRequest, WikiBuildService=FakeService)
    monkeypatch.setattr(runner, "_load_modules", lambda: (config_module, service_module))

    build_id = runner.stage()

    assert build_id == "build-0123456789abcdefabcd"
    assert [entry[0] for entry in calls] == ["service", "enrich", "stage"]
    assert calls[1][2] is False


def test_runner_can_defer_startup_enrichment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner(tmp_path)
    calls: list[str] = []

    class FakeRequest:
        def __init__(self, **values: object) -> None:
            self.values = values

    class FakeService:
        def __init__(self, config: object) -> None:
            del config

        def enrich_documents_if_configured(
            self,
            request: FakeRequest,
            *,
            refresh: bool,
        ) -> None:
            del request, refresh
            calls.append("enrich")

        def stage(self, request: FakeRequest) -> object:
            del request
            calls.append("stage")
            return SimpleNamespace(activated=False, build_id="build-deferred")

    modules = (
        SimpleNamespace(load_config=lambda _path: object()),
        SimpleNamespace(WikiBuildRequest=FakeRequest, WikiBuildService=FakeService),
    )
    monkeypatch.setattr(runner, "_load_modules", lambda: modules)
    monkeypatch.setenv("CODEGATE_LLMWIKI_STARTUP_ENRICHMENT", "deferred")

    assert runner.stage() == "build-deferred"
    assert calls == ["stage"]


def test_runner_preserves_previous_wiki_when_enrichment_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner(tmp_path)
    staged = False

    class FakeRequest:
        def __init__(self, **values: object) -> None:
            self.values = values

    class FakeService:
        def __init__(self, config: object) -> None:
            del config

        def enrich_documents_if_configured(
            self,
            request: FakeRequest,
            *,
            refresh: bool,
        ) -> None:
            del request, refresh
            raise RuntimeError("provider unavailable")

        def stage(self, request: FakeRequest) -> object:
            nonlocal staged
            del request
            staged = True
            return SimpleNamespace(activated=False, build_id="should-not-exist")

    modules = (
        SimpleNamespace(load_config=lambda _path: object()),
        SimpleNamespace(WikiBuildRequest=FakeRequest, WikiBuildService=FakeService),
    )
    monkeypatch.setattr(runner, "_load_modules", lambda: modules)

    with pytest.raises(LLMWikiCompatibilityError, match="provider unavailable"):
        runner.stage()

    assert staged is False


def test_runner_stages_canonical_indexes_when_enrichment_returns_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner(tmp_path)
    staged = False

    class FakeRequest:
        def __init__(self, **values: object) -> None:
            self.values = values

    class FakeService:
        def __init__(self, config: object) -> None:
            del config

        def enrich_documents_if_configured(
            self,
            request: FakeRequest,
            *,
            refresh: bool,
        ) -> object:
            del request, refresh
            return SimpleNamespace(failed=["DOC-FAILED"])

        def stage(self, request: FakeRequest) -> object:
            nonlocal staged
            del request
            staged = True
            return SimpleNamespace(activated=False, build_id="build-without-enrichment")

    modules = (
        SimpleNamespace(load_config=lambda _path: object()),
        SimpleNamespace(WikiBuildRequest=FakeRequest, WikiBuildService=FakeService),
    )
    monkeypatch.setattr(runner, "_load_modules", lambda: modules)

    assert runner.stage() == "build-without-enrichment"

    assert staged is True


def test_runner_stages_canonical_indexes_when_enrichment_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner(tmp_path)
    staged = False

    class ValidationError(Exception):
        pass

    class FakeRequest:
        def __init__(self, **values: object) -> None:
            self.values = values

    class FakeService:
        def __init__(self, config: object) -> None:
            del config

        def enrich_documents_if_configured(
            self,
            request: FakeRequest,
            *,
            refresh: bool,
        ) -> None:
            del request, refresh
            raise ValidationError("grounding mismatch")

        def stage(self, request: FakeRequest) -> object:
            nonlocal staged
            del request
            staged = True
            return SimpleNamespace(activated=False, build_id="build-canonical-only")

    modules = (
        SimpleNamespace(load_config=lambda _path: object()),
        SimpleNamespace(WikiBuildRequest=FakeRequest, WikiBuildService=FakeService),
    )
    monkeypatch.setattr(runner, "_load_modules", lambda: modules)

    assert runner.stage(require_enrichment=True) == "build-canonical-only"

    assert staged is True
