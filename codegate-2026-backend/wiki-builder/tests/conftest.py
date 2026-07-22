from __future__ import annotations

import pytest
import yaml

from wiki_builder.config import WikiConfig, load_config


@pytest.fixture(scope="session")
def config() -> WikiConfig:
    return load_config()


@pytest.fixture(scope="session")
def sample_expectations(config: WikiConfig) -> dict:
    path = config.project_dir / "tests" / "fixtures" / "sample-50.expectations.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))
