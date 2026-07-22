from __future__ import annotations

import json
from copy import deepcopy

import httpx
import pytest

import wiki_builder.gemini as gemini_module
from wiki_builder.config import WikiConfig, load_config
from wiki_builder.errors import ProviderError, ValidationError
from wiki_builder.gemini import GeminiClient, doctor, sanitize_json_schema
from wiki_builder.provenance import model_fingerprint


class FakeHttpClient:
    def __init__(self, responses: list[httpx.Response]):
        self.responses = responses
        self.requests: list[tuple[str, dict]] = []
        self.closed = False

    def post(self, path: str, json: dict) -> httpx.Response:
        self.requests.append((path, json))
        return self.responses.pop(0)

    def get(self, path: str) -> httpx.Response:
        self.requests.append((path, {}))
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[httpx.Response],
) -> FakeHttpClient:
    fake = FakeHttpClient(responses)
    monkeypatch.setattr(gemini_module, "get_api_key", lambda config, required=True: "secret")
    monkeypatch.setattr(gemini_module.httpx, "Client", lambda **kwargs: fake)
    return fake


def test_schema_is_reduced_to_gemini_supported_subset() -> None:
    source = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "doc_id": {"type": "string", "const": "REG-000001", "pattern": "^REG"},
            "keywords": {
                "type": "array",
                "uniqueItems": True,
                "minItems": 5,
                "items": {"type": "string", "minLength": 1},
            },
            "evidence": {"$ref": "#/$defs/evidence"},
        },
        "required": ["doc_id", "keywords", "evidence"],
        "$defs": {
            "evidence": {
                "type": "object",
                "properties": {"quote": {"type": "string"}},
                "required": ["quote"],
            }
        },
    }
    result = sanitize_json_schema(source)
    assert "$schema" not in result
    assert result["propertyOrdering"] == ["doc_id", "keywords", "evidence"]
    assert result["properties"]["doc_id"]["enum"] == ["REG-000001"]
    assert "pattern" not in result["properties"]["doc_id"]
    assert "uniqueItems" not in result["properties"]["keywords"]
    assert "minLength" not in result["properties"]["keywords"]["items"]
    assert result["properties"]["keywords"]["minItems"] == 5
    assert result["$defs"]["evidence"]["required"] == ["quote"]


def test_complete_sends_gemini_structured_output_request(
    config: WikiConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": '{"value":"ok"}'}]},
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 20,
                "thoughtsTokenCount": 5,
                "totalTokenCount": 125,
            },
        },
    )
    fake = _install_fake_client(monkeypatch, [response])
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["value"],
        "properties": {"value": {"type": "string"}},
    }
    with GeminiClient(config) as client:
        completion = client.complete("system", "user", schema)
    assert json.loads(completion.text) == {"value": "ok"}
    assert completion.usage["input_tokens"] == 100
    assert completion.usage["output_tokens"] == 25
    assert completion.usage["estimated_cost"] == 0.00002
    path, payload = fake.requests[0]
    assert path.endswith("/models/gemini-2.5-flash-lite:generateContent")
    assert payload["systemInstruction"]["parts"][0]["text"] == "system"
    generation = payload["generationConfig"]
    assert generation["responseMimeType"] == "application/json"
    assert generation["thinkingConfig"] == {"thinkingBudget": 0}
    assert "thinkingLevel" not in generation["thinkingConfig"]
    assert generation["responseJsonSchema"]["required"] == ["value"]
    assert fake.closed is True


def test_retryable_api_error_is_retried(
    config: WikiConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = [
        httpx.Response(429, json={"error": {"message": "quota"}}),
        httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": "{}"}]},
                    }
                ]
            },
        ),
    ]
    fake = _install_fake_client(monkeypatch, responses)
    monkeypatch.setattr(gemini_module.time, "sleep", lambda seconds: None)
    with GeminiClient(config) as client:
        assert client.complete("system", "user", {"type": "object"}).text == "{}"
    assert len(fake.requests) == 2


def test_retry_after_header_is_capped(
    config: WikiConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = httpx.Response(429, headers={"Retry-After": "3600"})
    sleeps: list[float] = []
    monkeypatch.setattr(gemini_module, "get_api_key", lambda config, required=True: "secret")
    monkeypatch.setattr(gemini_module.httpx, "Client", lambda **kwargs: FakeHttpClient([]))
    monkeypatch.setattr(gemini_module.time, "sleep", sleeps.append)

    with GeminiClient(config) as client:
        client._sleep_before_retry(response, 1)  # noqa: SLF001

    assert sleeps == [30.0]


def test_auth_error_does_not_expose_key(
    config: WikiConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_fake_client(
        monkeypatch,
        [httpx.Response(401, json={"error": {"message": "invalid credential"}})],
    )
    with GeminiClient(config) as client, pytest.raises(ProviderError) as captured:
        client.complete("system", "user", {"type": "object"})
    assert "secret" not in str(captured.value)
    assert len(fake.requests) == 1


def test_provider_fingerprint_changes_with_model_config(config: WikiConfig) -> None:
    changed_raw = deepcopy(config.raw)
    changed_raw["provider"]["model"] = "different-model"
    changed = WikiConfig(path=config.path, raw=changed_raw)
    assert model_fingerprint(config) != model_fingerprint(changed)


def test_provider_fingerprint_changes_with_thinking_budget(config: WikiConfig) -> None:
    changed_raw = deepcopy(config.raw)
    changed_raw["provider"]["thinking_budget"] = 128
    changed = WikiConfig(path=config.path, raw=changed_raw)
    assert model_fingerprint(config) != model_fingerprint(changed)


def test_data_policy_defaults_to_checked_in_development_value(
    config: WikiConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEGATE_GEMINI_DATA_POLICY", raising=False)

    loaded = load_config(config.path)

    assert loaded.provider["data_policy"] == "development-free"


def test_data_policy_accepts_only_explicit_supported_environment_literal(
    config: WikiConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEGATE_GEMINI_DATA_POLICY", "paid-no-training")

    loaded = load_config(config.path)

    assert loaded.provider["data_policy"] == "paid-no-training"


@pytest.mark.parametrize("value", ["paid", "PAID-NO-TRAINING", " paid-no-training "])
def test_data_policy_rejects_ambiguous_environment_values(
    config: WikiConfig,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("CODEGATE_GEMINI_DATA_POLICY", value)

    with pytest.raises(ValidationError, match="CODEGATE_GEMINI_DATA_POLICY"):
        load_config(config.path)


def test_doctor_accepts_the_selected_model_configuration(
    config: WikiConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gemini_module, "get_api_key", lambda config, required=False: None)

    report = doctor(config)

    provider = report["checks"]["provider_config"]
    assert provider["ok"] is True
    assert provider["requested_model"] == "gemini-2.5-flash-lite"
    assert provider["model"] == "gemini-2.5-flash-lite"


def test_env_example_exists_and_secrets_are_ignored(config: WikiConfig) -> None:
    env_file = config.resolve_path("env_file")
    env_example = env_file.with_name(".env.example")
    assert env_example.is_file()
    assert env_example.read_text(encoding="utf-8").splitlines() == ["GEMINI_API_KEY="]
    ignore_rules = (config.project_dir / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignore_rules
    assert "!.env.example" in ignore_rules
