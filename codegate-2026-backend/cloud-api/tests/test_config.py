import pytest
from pydantic import ValidationError

import codegate_cloud_api.main as main_module
from codegate_cloud_api.config import Settings


def test_production_requires_google_client_and_session_pepper() -> None:
    with pytest.raises(ValidationError, match="CODEGATE_GOOGLE_CLIENT_ID"):
        Settings(environment="production")


def test_production_rejects_wildcard_cors() -> None:
    with pytest.raises(ValidationError, match="CORS origin allowlist"):
        Settings(
            environment="production",
            google_client_id="client.apps.googleusercontent.com",
            session_pepper="x" * 32,
            cors_origins=["*"],
        )


def test_production_rejects_insecure_google_endpoint() -> None:
    with pytest.raises(ValidationError, match="must use HTTPS"):
        Settings(
            environment="production",
            google_client_id="client.apps.googleusercontent.com",
            session_pepper="x" * 32,
            google_token_endpoint="http://oauth.example/token",
        )


def test_api_bind_defaults_and_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEGATE_API_HOST", raising=False)
    monkeypatch.delenv("CODEGATE_API_PORT", raising=False)
    settings = Settings(_env_file=None)
    assert settings.api_host == "127.0.0.1"
    assert settings.api_port == 8000

    monkeypatch.setenv("CODEGATE_API_HOST", "127.0.0.2")
    monkeypatch.setenv("CODEGATE_API_PORT", "9123")
    configured = Settings(_env_file=None)
    assert configured.api_host == "127.0.0.2"
    assert configured.api_port == 9123


def test_api_port_is_validated() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, api_port=65_536)


def test_run_uses_configured_bind_address(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        api_host="0.0.0.0",
        api_port=9123,
    )
    app = object()
    captured: dict[str, object] = {}
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "create_app", lambda value: app)

    def fake_run(value, *, host: str, port: int) -> None:  # type: ignore[no-untyped-def]
        captured.update(app=value, host=host, port=port)

    monkeypatch.setattr(main_module.uvicorn, "run", fake_run)

    main_module.run()

    assert captured == {"app": app, "host": "0.0.0.0", "port": 9123}
