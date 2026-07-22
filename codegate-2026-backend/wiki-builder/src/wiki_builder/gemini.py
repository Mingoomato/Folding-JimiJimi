from __future__ import annotations

import math
import os
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import httpx
from dotenv import load_dotenv

from wiki_builder.config import WikiConfig
from wiki_builder.errors import ProviderError

RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
MAX_RETRY_DELAY_SECONDS = 30.0
SUPPORTED_JSON_SCHEMA_KEYS = {
    "$id",
    "$defs",
    "$ref",
    "$anchor",
    "type",
    "format",
    "title",
    "description",
    "enum",
    "items",
    "prefixItems",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "anyOf",
    "oneOf",
    "properties",
    "additionalProperties",
    "required",
    "propertyOrdering",
}


@dataclass(frozen=True)
class GeminiCompletion:
    text: str
    usage: dict[str, Any]


def get_api_key(config: WikiConfig, required: bool = True) -> str | None:
    env_file = config.resolve_path("env_file")
    load_dotenv(dotenv_path=env_file, override=False)
    variable = str(config.provider["api_key_env"])
    value = os.environ.get(variable, "").strip()
    if value:
        return value
    if required:
        raise ProviderError(f"{env_file}의 {variable}에 Gemini API 키를 입력하세요.")
    return None


def sanitize_json_schema(value: Any) -> Any:
    """Convert Draft 2020-12 schema to Gemini's supported JSON Schema subset."""
    if isinstance(value, list):
        return [sanitize_json_schema(item) for item in value]
    if not isinstance(value, dict):
        return deepcopy(value)
    result: dict[str, Any] = {}
    if "const" in value:
        result["enum"] = [deepcopy(value["const"])]
    for key, item in value.items():
        if key == "const" or key not in SUPPORTED_JSON_SCHEMA_KEYS:
            continue
        if key in {"properties", "$defs"} and isinstance(item, dict):
            result[key] = {name: sanitize_json_schema(schema) for name, schema in item.items()}
            if key == "properties":
                result["propertyOrdering"] = list(item)
        else:
            result[key] = sanitize_json_schema(item)
    return result


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
        message = payload.get("error", {}).get("message")
        if message:
            return str(message)[:500]
    except ValueError:
        pass
    return response.text[:500] or "응답 본문 없음"


class GeminiClient:
    def __init__(self, config: WikiConfig):
        self.config = config
        self.api_key = get_api_key(config, required=True)
        self.client = httpx.Client(
            base_url=str(config.provider["base_url"]).rstrip("/"),
            headers={
                "x-goog-api-key": self.api_key,
                "Content-Type": "application/json",
            },
            timeout=float(config.provider["request_timeout_seconds"]),
        )

    def __enter__(self) -> GeminiClient:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.client.close()

    @property
    def model_path(self) -> str:
        return f"/{self.config.provider['api_version']}/models/{self.config.provider['model']}"

    def _sleep_before_retry(self, response: httpx.Response | None, attempt: int) -> None:
        retry_after: float | None = None
        if response is not None:
            header = response.headers.get("Retry-After")
            if header:
                try:
                    retry_after = float(header)
                except ValueError:
                    retry_after = None
                if retry_after is not None and (not math.isfinite(retry_after) or retry_after < 0):
                    retry_after = None
        base = float(self.config.provider["retry_base_seconds"])
        delay = retry_after if retry_after is not None else base * (2 ** (attempt - 1))
        time.sleep(min(delay, MAX_RETRY_DELAY_SECONDS))

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        max_attempts = int(self.config.provider["api_max_attempts"])
        last_error = ""
        for attempt in range(1, max_attempts + 1):
            response: httpx.Response | None = None
            try:
                response = self.client.post(f"{self.model_path}:generateContent", json=payload)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = f"네트워크 오류: {type(exc).__name__}"
                if attempt < max_attempts:
                    self._sleep_before_retry(None, attempt)
                    continue
                raise ProviderError(f"Gemini API 요청 실패: {last_error}") from exc
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ProviderError("Gemini API가 잘못된 JSON 응답을 반환했습니다") from exc
            message = _error_message(response)
            last_error = f"HTTP {response.status_code}: {message}"
            if response.status_code in RETRYABLE_STATUS_CODES and attempt < max_attempts:
                self._sleep_before_retry(response, attempt)
                continue
            if response.status_code in {401, 403}:
                raise ProviderError("Gemini API 키 인증 또는 모델 접근 권한을 확인하세요")
            if response.status_code == 404:
                raise ProviderError(
                    f"Gemini 모델을 찾을 수 없습니다: {self.config.provider['model']}"
                )
            raise ProviderError(f"Gemini API 요청 실패: {last_error}")
        raise ProviderError(f"Gemini API 요청 실패: {last_error}")

    def check_model_access(self) -> dict[str, Any]:
        try:
            response = self.client.get(self.model_path)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Gemini API 연결 실패: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise ProviderError(
                f"Gemini 모델 접근 확인 실패: HTTP {response.status_code}: "
                f"{_error_message(response)}"
            )
        value = response.json()
        return {
            "name": value.get("name"),
            "version": value.get("version"),
            "display_name": value.get("displayName"),
        }

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict[str, Any],
    ) -> GeminiCompletion:
        provider = self.config.provider
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [
                {"role": "user", "parts": [{"text": user_prompt}]},
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": sanitize_json_schema(response_schema),
                "temperature": provider["temperature"],
                "topP": provider["top_p"],
                "topK": provider["top_k"],
                "seed": provider["seed"],
                "maxOutputTokens": provider["max_output_tokens"],
                "candidateCount": 1,
                "thinkingConfig": {
                    "thinkingBudget": provider["thinking_budget"],
                },
            },
        }
        result = self._post(payload)
        candidates = result.get("candidates") or []
        if not candidates:
            block_reason = result.get("promptFeedback", {}).get("blockReason", "unknown")
            raise ProviderError(f"Gemini 응답 후보가 없습니다: block_reason={block_reason}")
        candidate = candidates[0]
        finish_reason = candidate.get("finishReason")
        if finish_reason not in {None, "STOP"}:
            raise ProviderError(f"Gemini 생성이 정상 종료되지 않았습니다: {finish_reason}")
        parts = candidate.get("content", {}).get("parts", [])
        text = "".join(str(part["text"]) for part in parts if part.get("text"))
        if not text.strip():
            raise ProviderError("Gemini가 빈 텍스트를 반환했습니다")
        metadata = result.get("usageMetadata", {})
        input_tokens = int(metadata.get("promptTokenCount", 0) or 0)
        candidate_tokens = int(metadata.get("candidatesTokenCount", 0) or 0)
        thought_tokens = int(metadata.get("thoughtsTokenCount", 0) or 0)
        output_tokens = candidate_tokens + thought_tokens
        total_tokens = int(
            metadata.get("totalTokenCount", input_tokens + output_tokens)
            or input_tokens + output_tokens
        )
        pricing = provider["pricing"]
        estimated_cost = (
            input_tokens * float(pricing["input_per_million_tokens"])
            + output_tokens * float(pricing["output_per_million_tokens"])
        ) / 1_000_000
        return GeminiCompletion(
            text=text,
            usage={
                "input_tokens": input_tokens,
                "candidate_tokens": candidate_tokens,
                "thought_tokens": thought_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "estimated_cost": round(estimated_cost, 8),
                "currency": pricing["currency"],
                "pricing_effective_on": str(pricing["effective_on"]),
            },
        )


def doctor(config: WikiConfig) -> dict[str, Any]:
    env_file = config.resolve_path("env_file")
    key = get_api_key(config, required=False)
    provider_valid = all(
        (
            config.provider["name"] == "google-gemini",
            config.provider["api"] == "generateContent",
            config.provider["requested_model"] == "gemini-2.5-flash-lite",
            config.provider["model"] == "gemini-2.5-flash-lite",
            config.provider["thinking_budget"] == 0,
            str(config.provider["base_url"]).startswith("https://"),
            config.provider["data_policy"]
            in {"development-free", config.security["paid_policy_name"]},
        )
    )
    access: dict[str, Any] = {"ok": False, "model": None, "error": None}
    if key:
        try:
            with GeminiClient(config) as client:
                access = {"ok": True, "model": client.check_model_access(), "error": None}
        except ProviderError as exc:
            access["error"] = str(exc)
    checks = {
        "env_file": {"ok": env_file.is_file(), "path": str(env_file)},
        "api_key": {
            "ok": key is not None,
            "variable": config.provider["api_key_env"],
            "value_exposed": False,
        },
        "provider_config": {
            "ok": provider_valid,
            "provider": config.provider["name"],
            "requested_model": config.provider["requested_model"],
            "model": config.provider["model"],
            "migration_reason": config.provider["migration_reason"],
            "data_policy": config.provider["data_policy"],
        },
        "model_access": access,
    }
    return {"ok": all(check["ok"] for check in checks.values()), "checks": checks}
