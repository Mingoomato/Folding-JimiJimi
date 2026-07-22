from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codegate_api.config import Settings
from codegate_api.main import create_app

DEMO_TOKEN = "demo-token-for-tests-only"  # gitleaks:allow
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def app_settings(tmp_path: Path) -> Settings:
    return Settings(
        seed_knowledge_root=PROJECT_ROOT / "demo/llm-wiki",
        seed_source_root=PROJECT_ROOT / "demo/source",
        runtime_root=tmp_path,
        source_root=tmp_path / "source",
        knowledge_releases_root=tmp_path / "knowledge/releases",
        knowledge_pointer_path=tmp_path / "knowledge/CURRENT",
        database_path=tmp_path / "state.sqlite3",
        backup_root=tmp_path / "backups",
        agent_state_root=tmp_path / "claude",
        auth_mode="demo",
        demo_auth_token=DEMO_TOKEN,
    )


@pytest.fixture
def test_app(app_settings: Settings) -> FastAPI:
    return create_app(app_settings)


@pytest.fixture
def client(test_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(test_app) as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {DEMO_TOKEN}"}
