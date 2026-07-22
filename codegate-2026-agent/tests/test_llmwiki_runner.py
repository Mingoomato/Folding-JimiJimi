from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from codegate_api.knowledge.llmwiki import (
    InProcessLLMWikiBuildRunner,
    LLMWikiCompatibilityError,
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


def test_runner_preserves_previous_wiki_when_enrichment_returns_failures(
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
            return SimpleNamespace(activated=False, build_id="should-not-exist")

    modules = (
        SimpleNamespace(load_config=lambda _path: object()),
        SimpleNamespace(WikiBuildRequest=FakeRequest, WikiBuildService=FakeService),
    )
    monkeypatch.setattr(runner, "_load_modules", lambda: modules)

    with pytest.raises(LLMWikiCompatibilityError, match="DOC-FAILED"):
        runner.stage()

    assert staged is False
