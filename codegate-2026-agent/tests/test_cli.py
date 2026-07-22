from pathlib import Path

import pytest

from codegate_api.cli import _parser, _settings, main


def test_local_cli_builds_fail_closed_product_settings(tmp_path: Path) -> None:
    source = tmp_path / "source"
    project = tmp_path / "project"
    input_root = tmp_path / "input"
    storage = tmp_path / "storage"
    data = tmp_path / "data"
    for path in (source, project, input_root):
        path.mkdir()
    args = _parser().parse_args(
        [
            "--source-root",
            str(source),
            "--llmwiki-project-root",
            str(project),
            "--llmwiki-input-root",
            str(input_root),
            "--llmwiki-storage-root",
            str(storage),
            "--data-root",
            str(data),
            "--cors-origin",
            "http://127.0.0.1:4173",
        ]
    )

    settings = _settings(args)

    assert settings.environment == "local"
    assert settings.auth_mode == "local"
    assert settings.agent_mode == "gemini"
    assert settings.knowledge_mode == "llmwiki"
    assert settings.bootstrap_demo is False
    assert settings.cors_origins == ["http://127.0.0.1:4173"]
    assert storage.is_dir()


@pytest.mark.parametrize("agent_mode", ["gemini", "claude", "deterministic"])
def test_local_cli_accepts_every_supported_agent_mode(tmp_path: Path, agent_mode: str) -> None:
    roots = [tmp_path / name for name in ("source", "project", "input")]
    for root in roots:
        root.mkdir()
    args = _parser().parse_args(
        [
            "--source-root",
            str(roots[0]),
            "--llmwiki-project-root",
            str(roots[1]),
            "--llmwiki-input-root",
            str(roots[2]),
            "--llmwiki-storage-root",
            str(tmp_path / "storage"),
            "--agent-mode",
            agent_mode,
        ]
    )

    assert _settings(args).agent_mode == agent_mode


def test_deterministic_agent_flag_remains_compatible(tmp_path: Path) -> None:
    roots = [tmp_path / name for name in ("source", "project", "input")]
    for root in roots:
        root.mkdir()
    args = _parser().parse_args(
        [
            "--source-root",
            str(roots[0]),
            "--llmwiki-project-root",
            str(roots[1]),
            "--llmwiki-input-root",
            str(roots[2]),
            "--llmwiki-storage-root",
            str(tmp_path / "storage"),
            "--agent-mode",
            "claude",
            "--deterministic-agent",
        ]
    )

    assert _settings(args).agent_mode == "deterministic"


def test_local_cli_rejects_non_loopback_bind() -> None:
    with pytest.raises(SystemExit, match="only binds to a loopback"):
        main(
            [
                "--source-root",
                "/missing/source",
                "--llmwiki-project-root",
                "/missing/project",
                "--llmwiki-input-root",
                "/missing/input",
                "--llmwiki-storage-root",
                "/missing/storage",
                "--host",
                "0.0.0.0",
            ]
        )
