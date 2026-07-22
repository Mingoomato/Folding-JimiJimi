from __future__ import annotations

import os
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from wiki_builder.errors import ValidationError

SCOPE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
GEMINI_DATA_POLICY_ENV = "CODEGATE_GEMINI_DATA_POLICY"
GEMINI_DATA_POLICIES = frozenset({"development-free", "paid-no-training"})


@dataclass(frozen=True)
class WikiScope:
    tenant_id: str
    wiki_id: str
    input_dir: Path
    storage_root: Path
    synthetic_corpus: bool = False
    actor_id: str = "system"

    def __post_init__(self) -> None:
        for label, value in (
            ("tenant_id", self.tenant_id),
            ("wiki_id", self.wiki_id),
            ("actor_id", self.actor_id),
        ):
            if not SCOPE_ID_RE.fullmatch(value):
                raise ValidationError(f"잘못된 {label}: {value!r}")

    @property
    def wiki_root(self) -> Path:
        return self.storage_root.resolve() / "tenants" / self.tenant_id / "wikis" / self.wiki_id

    @property
    def builds_dir(self) -> Path:
        return self.wiki_root / "builds"

    @property
    def enrichment_dir(self) -> Path:
        return self.wiki_root / "enrichment"

    @property
    def current_pointer(self) -> Path:
        return self.wiki_root / "current.json"


@dataclass(frozen=True)
class WikiConfig:
    path: Path
    raw: dict[str, Any]

    @property
    def project_dir(self) -> Path:
        return self.path.parent.parent

    def resolve_path(self, key: str) -> Path:
        value = self.raw["paths"][key]
        path = Path(value)
        if not path.is_absolute():
            path = self.project_dir / path
        return path.resolve()

    @property
    def schema_version(self) -> str:
        return str(self.raw["schema_version"])

    @property
    def wiki_version(self) -> str:
        return str(self.raw["wiki_version"])

    @property
    def prompt_version(self) -> str:
        return str(self.raw["prompt_version"])

    @property
    def provider(self) -> dict[str, Any]:
        return self.raw["provider"]

    @property
    def enrichment(self) -> dict[str, Any]:
        return self.raw["enrichment"]

    @property
    def input_limits(self) -> dict[str, Any]:
        return self.raw["input_limits"]

    @property
    def security(self) -> dict[str, Any]:
        return self.raw["security"]

    @property
    def scope(self) -> dict[str, Any]:
        return self.raw.get("scope", {})


def default_scope(config: WikiConfig) -> WikiScope:
    defaults = config.raw["defaults"]
    return WikiScope(
        tenant_id=str(defaults["tenant_id"]),
        wiki_id=str(defaults["wiki_id"]),
        input_dir=config.resolve_path("input_dir"),
        storage_root=config.resolve_path("storage_root"),
        synthetic_corpus=bool(defaults.get("synthetic_corpus", False)),
        actor_id="local-user",
    )


def scoped_config(config: WikiConfig, scope: WikiScope) -> WikiConfig:
    raw = deepcopy(config.raw)
    raw["paths"]["input_dir"] = str(scope.input_dir.resolve())
    raw["paths"]["enrichment_dir"] = str(scope.enrichment_dir)
    raw["paths"]["output_dir"] = str(scope.wiki_root)
    raw["scope"] = {
        "tenant_id": scope.tenant_id,
        "wiki_id": scope.wiki_id,
        "synthetic_corpus": scope.synthetic_corpus,
        "actor_id": scope.actor_id,
        "storage_root": str(scope.storage_root.resolve()),
    }
    return WikiConfig(path=config.path, raw=raw)


def default_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "wiki.yaml"


def _require_mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValidationError(f"wiki.yaml의 {key}는 객체여야 합니다")
    return value


def _require_keys(name: str, value: dict[str, Any], required: set[str]) -> None:
    missing = sorted(required - value.keys())
    if missing:
        raise ValidationError(f"wiki.yaml {name} 필수 키가 없습니다: {', '.join(missing)}")


def _positive_integer(name: str, value: Any, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"wiki.yaml {name}은 정수여야 합니다")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise ValidationError(f"wiki.yaml {name}은 {minimum} 이상이어야 합니다")
    return value


def _validate_config(data: dict[str, Any]) -> None:
    paths = _require_mapping(data, "paths")
    defaults = _require_mapping(data, "defaults")
    normalization = _require_mapping(data, "normalization")
    chunking = _require_mapping(data, "chunking")
    provider = _require_mapping(data, "provider")
    security = _require_mapping(data, "security")
    enrichment = _require_mapping(data, "enrichment")
    limits = _require_mapping(data, "input_limits")
    _require_keys(
        "paths",
        paths,
        {
            "input_dir",
            "storage_root",
            "schemas_dir",
            "prompt_file",
            "enrichment_dir",
            "env_file",
        },
    )
    _require_keys("defaults", defaults, {"tenant_id", "wiki_id", "synthetic_corpus"})
    _require_keys(
        "normalization",
        normalization,
        {"unicode", "line_endings", "section_anchor_prefix", "section_anchor_hash_length"},
    )
    _require_keys("chunking", chunking, {"max_chars", "overlap_chars"})
    _require_keys(
        "provider",
        provider,
        {
            "name",
            "api",
            "api_version",
            "base_url",
            "requested_model",
            "model",
            "migration_reason",
            "api_key_env",
            "temperature",
            "top_p",
            "top_k",
            "seed",
            "max_output_tokens",
            "thinking_budget",
            "request_timeout_seconds",
            "api_max_attempts",
            "retry_base_seconds",
            "data_policy",
            "pricing",
        },
    )
    _require_keys(
        "security",
        security,
        {
            "require_paid_policy_for_nonpublic",
            "paid_policy_name",
            "external_llm_allowed_access",
            "block_restricted",
        },
    )
    _require_keys(
        "enrichment",
        enrichment,
        {
            "max_attempts",
            "summary_min_sentences",
            "summary_max_sentences",
            "key_points_min",
            "key_points_max",
            "keywords_min",
            "keywords_max",
            "faq_min",
            "faq_max",
            "relation_types",
        },
    )
    _require_keys(
        "input_limits",
        limits,
        {
            "max_documents",
            "max_source_files",
            "max_markdown_file_bytes",
            "max_total_markdown_bytes",
            "max_sections_per_document",
            "max_semantic_chunk_chars",
        },
    )
    max_chars = _positive_integer("chunking.max_chars", chunking["max_chars"])
    overlap = _positive_integer(
        "chunking.overlap_chars", chunking["overlap_chars"], allow_zero=True
    )
    if overlap >= max_chars:
        raise ValidationError("wiki.yaml chunking.overlap_chars는 max_chars보다 작아야 합니다")
    for key in (
        "max_documents",
        "max_source_files",
        "max_markdown_file_bytes",
        "max_total_markdown_bytes",
        "max_sections_per_document",
        "max_semantic_chunk_chars",
    ):
        _positive_integer(f"input_limits.{key}", limits[key])
    if limits["max_total_markdown_bytes"] < limits["max_markdown_file_bytes"]:
        raise ValidationError(
            "wiki.yaml input_limits.max_total_markdown_bytes는 파일당 한도 이상이어야 합니다"
        )
    if not str(provider["base_url"]).startswith("https://"):
        raise ValidationError("wiki.yaml provider.base_url은 HTTPS여야 합니다")
    if provider["data_policy"] not in GEMINI_DATA_POLICIES:
        raise ValidationError(
            "wiki.yaml provider.data_policy는 development-free 또는 paid-no-training이어야 합니다"
        )
    pricing = provider["pricing"]
    if not isinstance(pricing, dict):
        raise ValidationError("wiki.yaml provider.pricing은 객체여야 합니다")
    _require_keys(
        "provider.pricing",
        pricing,
        {
            "currency",
            "effective_on",
            "input_per_million_tokens",
            "output_per_million_tokens",
        },
    )
    _positive_integer("provider.api_max_attempts", provider["api_max_attempts"])
    _positive_integer("provider.request_timeout_seconds", provider["request_timeout_seconds"])
    _positive_integer("provider.thinking_budget", provider["thinking_budget"], allow_zero=True)
    _positive_integer("enrichment.max_attempts", enrichment["max_attempts"])
    allowed_access = security["external_llm_allowed_access"]
    if not isinstance(allowed_access, list) or not set(allowed_access) <= {
        "public",
        "internal",
        "restricted",
    }:
        raise ValidationError("wiki.yaml security.external_llm_allowed_access가 잘못되었습니다")


def load_config(path: Path | None = None) -> WikiConfig:
    config_path = (path or default_config_path()).resolve()
    if not config_path.is_file():
        raise ValidationError(f"설정 파일을 찾을 수 없습니다: {config_path}")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValidationError("wiki.yaml의 최상위 값은 객체여야 합니다.")
    required = {
        "schema_version",
        "wiki_version",
        "prompt_version",
        "paths",
        "defaults",
        "normalization",
        "chunking",
        "provider",
        "security",
        "enrichment",
        "input_limits",
    }
    missing = sorted(required - data.keys())
    if missing:
        raise ValidationError(f"wiki.yaml 필수 키가 없습니다: {', '.join(missing)}")
    _validate_config(data)
    data_policy_override = os.environ.get(GEMINI_DATA_POLICY_ENV)
    if data_policy_override not in {None, ""}:
        if data_policy_override not in GEMINI_DATA_POLICIES:
            allowed = ", ".join(sorted(GEMINI_DATA_POLICIES))
            raise ValidationError(
                f"{GEMINI_DATA_POLICY_ENV}는 정확히 다음 중 하나여야 합니다: {allowed}"
            )
        data["provider"]["data_policy"] = data_policy_override
    return WikiConfig(path=config_path, raw=data)
