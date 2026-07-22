import tomllib
from pathlib import Path

import pytest

from codegate_api.auth import LocalAuthenticator, authenticate_authorization_header
from codegate_api.config import Settings
from codegate_api.files.resolver import SourceUriResolver
from codegate_api.knowledge.catalog import KnowledgeCatalog, KnowledgeCatalogError
from codegate_api.runtime_workspace import RuntimeWorkspace


def _production_settings(volume: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "production",
        "auth_mode": "supabase",
        "agent_mode": "claude",
        "claude_model": "claude-pinned-test-model",
        "doc2md_base_url": "http://doc2md.internal",
        "doc2md_api_token": "production-doc2md-token",
        "bootstrap_demo": False,
        "supabase_url": "https://project.supabase.co",
        "RAILWAY_VOLUME_MOUNT_PATH": volume,
    }
    values.update(overrides)
    return Settings.model_validate(values)


def _local_settings(root: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "local",
        "auth_mode": "local",
        "knowledge_mode": "llmwiki",
        "bootstrap_demo": False,
        "source_root": root / "source",
        "llmwiki_project_root": root / "project",
        "llmwiki_input_root": root / "input",
        "llmwiki_storage_root": root / "storage",
        "cors_origins": ["http://127.0.0.1:3000"],
    }
    values.update(overrides)
    return Settings.model_validate(values)


def test_local_auth_is_loopback_workspace_capability(tmp_path: Path) -> None:
    context = authenticate_authorization_header(
        None,
        LocalAuthenticator(_local_settings(tmp_path)),
    )

    assert context.subject_id == "local-user"
    assert context.tenant_id == "local"
    assert context.provisioned is True
    assert context.allow_all_writes is True
    assert context.authz_source == "local-workspace"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"auth_mode": "supabase"}, "AUTH_MODE=local"),
        ({"bootstrap_demo": True}, "BOOTSTRAP_DEMO=false"),
        ({"knowledge_mode": "codegate"}, "KNOWLEDGE_MODE=llmwiki"),
        ({"cors_origins": ["*"]}, "explicit CORS origin"),
        (
            {"llmwiki_input_root": Path("source/normalized")},
            "must not overlap",
        ),
    ],
)
def test_local_mode_rejects_unsafe_configuration(
    tmp_path: Path,
    overrides: dict[str, object],
    message: str,
) -> None:
    if "llmwiki_input_root" in overrides:
        overrides = {
            **overrides,
            "llmwiki_input_root": tmp_path / Path(str(overrides["llmwiki_input_root"])),
        }
    with pytest.raises(ValueError, match=message):
        _local_settings(tmp_path, **overrides)


def test_railway_volume_maps_every_mutable_runtime_path(tmp_path: Path) -> None:
    volume = tmp_path / "volume"
    settings = _production_settings(volume)

    assert settings.resolved_runtime_root() == volume.resolve()
    assert settings.resolved_source_root() == (volume / "source").resolve()
    assert settings.resolved_database_path() == (volume / "codegate.sqlite3").resolve()
    assert settings.resolved_backup_root() == (volume / "backups").resolve()
    assert settings.resolved_knowledge_releases_root() == (volume / "knowledge/releases").resolve()
    assert settings.resolved_agent_state_root() == (volume / "claude").resolve()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"auth_mode": "disabled"}, "AUTH_MODE=supabase"),
        ({"supabase_url": None}, "Supabase issuer and JWKS"),
        ({"agent_mode": "deterministic"}, "network-backed CODEGATE_AGENT_MODE"),
        ({"claude_model": None}, "CLAUDE_MODEL"),
        ({"doc2md_base_url": None}, "DOC2MD_BASE_URL"),
        ({"doc2md_api_token": None}, "DOC2MD_API_TOKEN"),
        ({"bootstrap_demo": True}, "BOOTSTRAP_DEMO=false"),
        ({"cors_origins": ["*"]}, "explicit CORS origin"),
    ],
)
def test_production_rejects_unsafe_modes(
    tmp_path: Path,
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _production_settings(tmp_path / "volume", **overrides)


def test_production_bytes_transport_accepts_bounded_authenticated_source(
    tmp_path: Path,
) -> None:
    settings = _production_settings(
        tmp_path / "volume",
        doc2md_source_kind="bytes",
        doc2md_api_token="production-doc2md-token",
    )
    assert settings.doc2md_source_kind == "bytes"
    assert settings.doc2md_max_source_bytes == 33_554_432
    assert "production-doc2md-token" not in repr(settings)


def test_production_rejects_mutable_path_outside_volume(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mutable paths"):
        _production_settings(
            tmp_path / "volume",
            runtime_root=tmp_path / "outside",
            source_root=tmp_path / "outside/source",
        )


def test_production_startup_rejects_missing_volume_mount(tmp_path: Path) -> None:
    missing_volume = tmp_path / "missing-volume"
    workspace = RuntimeWorkspace(_production_settings(missing_volume))

    with pytest.raises(RuntimeError, match="persistent volume mount is missing"):
        workspace.bootstrap()


def test_production_cannot_fall_back_to_demo_knowledge_seed(tmp_path: Path) -> None:
    catalog = KnowledgeCatalog(
        seed_root=Path("demo/llm-wiki"),
        releases_root=tmp_path / "knowledge/releases",
        pointer_path=tmp_path / "knowledge/CURRENT",
        source_resolver=SourceUriResolver(tmp_path / "source"),
    )

    with pytest.raises(KnowledgeCatalogError, match="must be provisioned"):
        catalog.bootstrap(allow_seed_bootstrap=False)

    assert not (tmp_path / "knowledge/CURRENT").exists()


def test_supabase_and_railway_contract_files_are_fail_closed() -> None:
    migration = Path("supabase/migrations/202607210001_codegate_access_token_hook.sql").read_text(
        encoding="utf-8"
    )
    env_example = Path(".env.example").read_text(encoding="utf-8")
    railway = Path("railway.toml").read_text(encoding="utf-8")
    railway_config = tomllib.loads(railway)

    assert "grant usage on schema public to supabase_auth_admin" in migration
    assert "references public.codegate_user_access(user_id) on delete cascade" in migration
    assert "if access_row.user_id is null then" in migration
    assert "SUPABASE_SERVICE_ROLE_KEY" not in env_example
    assert "CODEGATE_SUPABASE_URL" in env_example
    assert "CODEGATE_CLAUDE_MODEL" in env_example
    assert "multiRegionConfig" not in railway
    assert "--workers" not in railway
    assert railway_config["deploy"]["startCommand"].startswith("sh -c ")
    assert "${PORT:-8000}" in railway_config["deploy"]["startCommand"]
