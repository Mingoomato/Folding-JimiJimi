from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="CODEGATE_",
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    api_host: str = Field(default="127.0.0.1", min_length=1)
    api_port: int = Field(default=8000, ge=1, le=65_535)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    database_path: Path = Path(".runtime/cloud-api.sqlite3")

    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    google_authorization_endpoint: str = "https://accounts.google.com/o/oauth2/v2/auth"
    google_token_endpoint: str = "https://oauth2.googleapis.com/token"
    google_jwks_url: str = "https://www.googleapis.com/oauth2/v3/certs"
    google_http_timeout_seconds: float = Field(default=10.0, ge=1.0, le=30.0)

    session_pepper: SecretStr | None = Field(default=None, min_length=32)
    access_token_ttl_seconds: int = Field(default=3_600, ge=300, le=86_400)
    refresh_token_ttl_seconds: int = Field(default=2_592_000, ge=3_600, le=7_776_000)
    oauth_flow_ttl_seconds: int = Field(default=300, ge=60, le=900)
    oauth_start_rate_limit_requests: int = Field(default=30, ge=1, le=1_000)
    oauth_start_rate_limit_window_seconds: int = Field(default=60, ge=1, le=3_600)
    refresh_rate_limit_requests: int = Field(default=120, ge=1, le=10_000)
    refresh_rate_limit_window_seconds: int = Field(default=60, ge=1, le=3_600)
    max_pending_oauth_flows: int = Field(default=10_000, ge=100, le=100_000)
    max_refresh_rotations_per_family: int = Field(default=2_048, ge=10, le=100_000)
    expired_data_cleanup_interval_seconds: int = Field(default=300, ge=10, le=86_400)
    authorization_header_max_bytes: int = Field(default=8_192, ge=1_024, le=32_768)

    @model_validator(mode="after")
    def validate_security_contract(self) -> "Settings":
        if self.environment == "production":
            if not self.google_client_id:
                raise ValueError("production requires CODEGATE_GOOGLE_CLIENT_ID")
            if self.session_pepper is None:
                raise ValueError("production requires CODEGATE_SESSION_PEPPER")
            if not self.cors_origins or "*" in self.cors_origins:
                raise ValueError("production requires an explicit CORS origin allowlist")
            for name, value in (
                ("authorization", self.google_authorization_endpoint),
                ("token", self.google_token_endpoint),
                ("JWKS", self.google_jwks_url),
            ):
                if urlsplit(value).scheme != "https":
                    raise ValueError(f"production Google {name} endpoint must use HTTPS")
        return self

    def resolved_session_pepper(self) -> bytes:
        if self.session_pepper is None:
            if self.environment == "test":
                return b"test-session-pepper-not-for-production"
            raise ValueError("CODEGATE_SESSION_PEPPER is required")
        return self.session_pepper.get_secret_value().encode()


@lru_cache
def get_settings() -> Settings:
    return Settings()
