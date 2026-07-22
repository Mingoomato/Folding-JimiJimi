from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="CODEGATE_",
        extra="ignore",
    )

    app_name: str = "CODEGATE 2026 API"
    environment: Literal["development", "test", "local", "production"] = "development"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    seed_knowledge_root: Path = Path("demo/llm-wiki")
    seed_source_root: Path = Path("demo/source")
    runtime_root: Path = Path(".runtime")
    source_root: Path = Path(".runtime/demo/source")
    knowledge_releases_root: Path = Path(".runtime/demo/knowledge/releases")
    knowledge_pointer_path: Path = Path(".runtime/demo/knowledge/CURRENT")
    database_path: Path = Path(".runtime/codegate.sqlite3")
    backup_root: Path = Path(".runtime/backups")
    agent_state_root: Path = Path(".runtime/claude")
    railway_volume_mount_path: Path | None = Field(
        default=None,
        validation_alias="RAILWAY_VOLUME_MOUNT_PATH",
    )
    bootstrap_demo: bool = True
    max_edit_bytes: int = Field(default=1_048_576, ge=1, le=10_485_760)
    plan_ttl_seconds: int = Field(default=900, ge=60, le=86_400)
    max_request_bytes: int = Field(default=1_200_000, ge=1_024, le=12_000_000)
    chat_rate_limit_requests: int = Field(default=60, ge=1, le=10_000)
    chat_rate_limit_window_seconds: int = Field(default=60, ge=1, le=3_600)
    auth_rate_limit_requests: int = Field(default=240, ge=1, le=20_000)
    auth_rate_limit_window_seconds: int = Field(default=60, ge=1, le=3_600)
    authorization_header_max_bytes: int = Field(default=8_192, ge=256, le=32_768)
    sync_stale_after_seconds: int = Field(default=30, ge=1, le=3_600)
    doc2md_base_url: str | None = None
    doc2md_timeout_seconds: float = Field(default=120.0, ge=1.0, le=600.0)
    doc2md_source_kind: Literal["path", "bytes"] = "path"
    doc2md_api_token: SecretStr | None = Field(default=None, min_length=16)
    doc2md_max_source_bytes: int = Field(default=33_554_432, ge=1, le=33_554_432)

    knowledge_mode: Literal["codegate", "llmwiki"] = "codegate"
    llmwiki_project_root: Path | None = None
    llmwiki_input_root: Path | None = None
    llmwiki_storage_root: Path | None = None
    llmwiki_tenant_id: str = Field(default="local", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    llmwiki_wiki_id: str = Field(
        default="workspace",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
    )
    llmwiki_actor_id: str = Field(
        default="codegate-local",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
    )

    agent_mode: Literal["deterministic", "claude", "gemini"] = "deterministic"
    claude_model: str | None = Field(default=None, min_length=1, pattern=r"^\S+$")
    claude_timeout_seconds: float = Field(default=45.0, ge=1.0, le=300.0)
    claude_session_ttl_seconds: int = Field(default=3_600, ge=60, le=86_400)
    gemini_model: str = Field(
        default="gemini-2.5-flash-lite",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    gemini_timeout_seconds: float = Field(default=45.0, ge=1.0, le=300.0)
    gemini_data_policy: Literal["development-free", "paid-no-training"] = "development-free"

    auth_mode: Literal["disabled", "demo", "local", "supabase"] = "disabled"
    demo_auth_token: str | None = None
    demo_subject_id: str = "demo-editor"
    demo_tenant_id: str = "demo"
    demo_read_access: list[str] = Field(default_factory=lambda: ["public", "internal"])
    demo_write_document_ids: list[str] = Field(
        default_factory=lambda: [
            "REG-000001",
            "REG-000002",
            "MAN-000001",
            "MAN-000002",
        ]
    )
    supabase_url: str | None = None
    supabase_jwt_issuer: str | None = None
    supabase_jwks_url: str | None = None
    supabase_jwks_timeout_seconds: float = Field(default=3.0, ge=0.5, le=10.0)
    supabase_jwt_audience: str = "authenticated"
    supabase_tenant_claim: str = "codegate_tenant_id"
    supabase_read_access_claim: str = "codegate_read_access"
    supabase_write_documents_claim: str = "codegate_write_document_ids"

    @model_validator(mode="after")
    def use_railway_volume_defaults(self) -> "Settings":
        mount = self.railway_volume_mount_path
        if mount is not None and self.runtime_root == Path(".runtime"):
            self.runtime_root = mount
            self.source_root = mount / "source"
            self.knowledge_releases_root = mount / "knowledge/releases"
            self.knowledge_pointer_path = mount / "knowledge/CURRENT"
            self.database_path = mount / "codegate.sqlite3"
            self.backup_root = mount / "backups"
            self.agent_state_root = mount / "claude"
        if self.knowledge_mode == "llmwiki":
            missing = [
                name
                for name, value in (
                    ("CODEGATE_LLMWIKI_PROJECT_ROOT", self.llmwiki_project_root),
                    ("CODEGATE_LLMWIKI_INPUT_ROOT", self.llmwiki_input_root),
                    ("CODEGATE_LLMWIKI_STORAGE_ROOT", self.llmwiki_storage_root),
                )
                if value is None
            ]
            if missing:
                raise ValueError("llmwiki knowledge mode requires " + ", ".join(missing))
        if self.environment == "local":
            if self.auth_mode != "local":
                raise ValueError("local environment requires CODEGATE_AUTH_MODE=local")
            if self.bootstrap_demo:
                raise ValueError("local environment requires CODEGATE_BOOTSTRAP_DEMO=false")
            if self.knowledge_mode != "llmwiki":
                raise ValueError("local environment requires CODEGATE_KNOWLEDGE_MODE=llmwiki")
            if not self.cors_origins or "*" in self.cors_origins:
                raise ValueError("local environment requires an explicit CORS origin allowlist")
            local_roots = {
                "source": self.resolved_source_root(),
                "LLMWIKI project": self.resolved_llmwiki_project_root(),
                "LLMWIKI input": self.resolved_llmwiki_input_root(),
                "LLMWIKI storage": self.resolved_llmwiki_storage_root(),
            }
            for left_name, left in local_roots.items():
                assert left is not None
                for right_name, right in local_roots.items():
                    assert right is not None
                    if left_name < right_name and (
                        left == right or left.is_relative_to(right) or right.is_relative_to(left)
                    ):
                        raise ValueError(
                            "local source, LLMWIKI project, input, and storage roots "
                            f"must not overlap: {left_name}, {right_name}"
                        )
        if self.environment == "production":
            if self.auth_mode != "supabase":
                raise ValueError("production requires CODEGATE_AUTH_MODE=supabase")
            if not self.supabase_url and not (self.supabase_jwt_issuer and self.supabase_jwks_url):
                raise ValueError("production requires Supabase issuer and JWKS configuration")
            if self.agent_mode == "deterministic":
                raise ValueError("production requires a network-backed CODEGATE_AGENT_MODE")
            if self.agent_mode == "claude" and not self.claude_model:
                raise ValueError("production requires CODEGATE_CLAUDE_MODEL")
            if not self.doc2md_base_url:
                raise ValueError("production requires CODEGATE_DOC2MD_BASE_URL")
            if not self.doc2md_api_token:
                raise ValueError("production requires CODEGATE_DOC2MD_API_TOKEN")
            if self.bootstrap_demo:
                raise ValueError("production requires CODEGATE_BOOTSTRAP_DEMO=false")
            if mount is None:
                raise ValueError("production requires RAILWAY_VOLUME_MOUNT_PATH")
            if not self.cors_origins or "*" in self.cors_origins:
                raise ValueError("production requires an explicit CORS origin allowlist")
            resolved_mount = mount.expanduser().resolve()
            mutable_paths = {
                self.resolved_runtime_root(),
                self.resolved_source_root(),
                self.resolved_knowledge_releases_root(),
                self.resolved_knowledge_pointer_path().parent,
                self.resolved_database_path().parent,
                self.resolved_backup_root(),
                self.resolved_agent_state_root(),
            }
            if any(not path.is_relative_to(resolved_mount) for path in mutable_paths):
                raise ValueError("all production mutable paths must be inside the Railway volume")
        return self

    def resolved_seed_knowledge_root(self) -> Path:
        return self.seed_knowledge_root.expanduser().resolve()

    def resolved_seed_source_root(self) -> Path:
        return self.seed_source_root.expanduser().resolve()

    def resolved_runtime_root(self) -> Path:
        return self.runtime_root.expanduser().resolve()

    def resolved_source_root(self) -> Path:
        return self.source_root.expanduser().resolve()

    def resolved_knowledge_releases_root(self) -> Path:
        return self.knowledge_releases_root.expanduser().resolve()

    def resolved_knowledge_pointer_path(self) -> Path:
        return self.knowledge_pointer_path.expanduser().resolve()

    def resolved_database_path(self) -> Path:
        return self.database_path.expanduser().resolve()

    def resolved_backup_root(self) -> Path:
        return self.backup_root.expanduser().resolve()

    def resolved_agent_state_root(self) -> Path:
        return self.agent_state_root.expanduser().resolve()

    def resolved_llmwiki_project_root(self) -> Path | None:
        return (
            self.llmwiki_project_root.expanduser().resolve()
            if self.llmwiki_project_root is not None
            else None
        )

    def resolved_llmwiki_input_root(self) -> Path | None:
        return (
            self.llmwiki_input_root.expanduser().resolve()
            if self.llmwiki_input_root is not None
            else None
        )

    def resolved_llmwiki_storage_root(self) -> Path | None:
        return (
            self.llmwiki_storage_root.expanduser().resolve()
            if self.llmwiki_storage_root is not None
            else None
        )

    def effective_supabase_issuer(self) -> str | None:
        if self.supabase_jwt_issuer:
            return self.supabase_jwt_issuer.rstrip("/")
        if self.supabase_url:
            return f"{self.supabase_url.rstrip('/')}/auth/v1"
        return None

    def effective_supabase_jwks_url(self) -> str | None:
        if self.supabase_jwks_url:
            return self.supabase_jwks_url
        issuer = self.effective_supabase_issuer()
        return f"{issuer}/.well-known/jwks.json" if issuer else None


@lru_cache
def get_settings() -> Settings:
    return Settings()
