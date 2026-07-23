from __future__ import annotations

import hashlib
import json

from wiki_builder.config import WikiConfig


def prompt_sha256(config: WikiConfig) -> str:
    return hashlib.sha256(config.resolve_path("prompt_file").read_bytes()).hexdigest()


def model_fingerprint(config: WikiConfig) -> str:
    """Stable provider fingerprint used to invalidate cached enrichments."""
    provider_keys = (
        "name",
        "api",
        "api_version",
        "base_url",
        "requested_model",
        "model",
        "temperature",
        "top_p",
        "top_k",
        "seed",
        "max_output_tokens",
        "thinking_budget",
    )
    material = {
        "provider": {key: config.provider[key] for key in provider_keys},
        "enrichment_input": {
            "max_input_chars": config.enrichment["max_input_chars"],
            "max_input_sections": config.enrichment["max_input_sections"],
        },
        "prompt_version": config.prompt_version,
        "prompt_sha256": prompt_sha256(config),
        "enrichment_schema_sha256": hashlib.sha256(
            (config.resolve_path("schemas_dir") / "enrichment.schema.json").read_bytes()
        ).hexdigest(),
    }
    serialized = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def model_display_name(config: WikiConfig) -> str:
    return f"google/{config.provider['model']}"


def compatible_model_fingerprint(config: WikiConfig, value: str) -> bool:
    accepted = set(config.raw.get("compatibility", {}).get("accepted_enrichment_fingerprints", []))
    accepted.add(model_fingerprint(config))
    return value in accepted
