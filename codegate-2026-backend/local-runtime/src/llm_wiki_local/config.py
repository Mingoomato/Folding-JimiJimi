from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _resolve(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


@dataclass(frozen=True)
class RuntimeSettings:
    config_path: Path
    state_dir: Path
    bootstrap_input_dir: Path
    storage_root: Path
    converter_url: str = "http://127.0.0.1:8123"
    tenant_id: str = "personal"
    wiki_id: str = "main"
    actor_id: str = "local-document-runtime"
    api_host: str = "127.0.0.1"
    api_port: int = 8765
    api_token: str | None = None
    allowed_origins: tuple[str, ...] = ()
    allowed_source_roots: tuple[Path, ...] = ()
    run_enrichment: bool = True
    synthetic_corpus: bool = False
    converter_timeout_seconds: float = 1800.0
    converter_poll_seconds: float = 0.5
    max_source_bytes: int = 512 * 1024 * 1024

    @property
    def database_path(self) -> Path:
        return self.state_dir / "state.sqlite3"

    @property
    def source_cache_dir(self) -> Path:
        return self.state_dir / "source-cache"

    @property
    def input_snapshots_dir(self) -> Path:
        return self.state_dir / "input-snapshots"

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> RuntimeSettings:
        runtime_dir = Path(__file__).resolve().parents[2]
        selected_env = env_file or runtime_dir / ".env"
        load_dotenv(selected_env, override=False)
        roots = tuple(
            _resolve(item.strip(), runtime_dir)
            for item in os.getenv("LLMWIKI_ALLOWED_SOURCE_ROOTS", "").split(",")
            if item.strip()
        )
        origins = tuple(
            item.strip()
            for item in os.getenv("LLMWIKI_ALLOWED_ORIGINS", "").split(",")
            if item.strip()
        )
        if "*" in origins:
            raise ValueError("LLMWIKI_ALLOWED_ORIGINS must not contain a wildcard")
        token = os.getenv("LLMWIKI_API_TOKEN", "").strip() or None
        settings = cls(
            config_path=_resolve(
                os.getenv("LLMWIKI_CONFIG_PATH", "../wiki-builder/config/wiki.yaml"),
                runtime_dir,
            ),
            state_dir=_resolve(os.getenv("LLMWIKI_STATE_DIR", "../local-data"), runtime_dir),
            bootstrap_input_dir=_resolve(
                os.getenv(
                    "LLMWIKI_BOOTSTRAP_INPUT_DIR", "../local-data/bootstrap-input"
                ),
                runtime_dir,
            ),
            storage_root=_resolve(
                os.getenv("LLMWIKI_STORAGE_ROOT", "../wiki-storage"), runtime_dir
            ),
            converter_url=os.getenv(
                "LLMWIKI_CONVERTER_URL", "http://127.0.0.1:8123"
            ).rstrip("/"),
            tenant_id=os.getenv("LLMWIKI_TENANT_ID", "personal"),
            wiki_id=os.getenv("LLMWIKI_WIKI_ID", "main"),
            actor_id=os.getenv("LLMWIKI_ACTOR_ID", "local-document-runtime"),
            api_host=os.getenv("LLMWIKI_API_HOST", "127.0.0.1"),
            api_port=int(os.getenv("LLMWIKI_API_PORT", "8765")),
            api_token=token,
            allowed_origins=origins,
            allowed_source_roots=roots,
            run_enrichment=_env_bool("LLMWIKI_RUN_ENRICHMENT", True),
            synthetic_corpus=_env_bool("LLMWIKI_SYNTHETIC_CORPUS", False),
            converter_timeout_seconds=float(
                os.getenv("LLMWIKI_CONVERTER_TIMEOUT_SECONDS", "1800")
            ),
            converter_poll_seconds=float(os.getenv("LLMWIKI_CONVERTER_POLL_SECONDS", "0.5")),
            max_source_bytes=int(os.getenv("LLMWIKI_MAX_SOURCE_BYTES", str(512 * 1024 * 1024))),
        )
        if settings.api_host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("LLMWIKI_API_HOST must be a loopback host")
        if settings.max_source_bytes <= 0:
            raise ValueError("LLMWIKI_MAX_SOURCE_BYTES must be positive")
        return settings
