from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from wiki_builder.config import WikiConfig
from wiki_builder.errors import ValidationError


@lru_cache(maxsize=16)
def _read_schema(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def schema_path(config: WikiConfig, name: str) -> Path:
    return config.resolve_path("schemas_dir") / f"{name}.schema.json"


def load_schema(config: WikiConfig, name: str) -> dict[str, Any]:
    path = schema_path(config, name)
    if not path.is_file():
        raise ValidationError(f"스키마 파일을 찾을 수 없습니다: {path}")
    return _read_schema(path)


def validate_record(config: WikiConfig, name: str, value: Any, label: str = "") -> None:
    validator = Draft202012Validator(load_schema(config, name), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(value), key=lambda item: list(item.absolute_path))
    if not errors:
        return
    formatted: list[str] = []
    for error in errors[:20]:
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        formatted.append(f"{location}: {error.message}")
    prefix = f"{label}: " if label else ""
    raise ValidationError(prefix + "스키마 검증 실패\n- " + "\n- ".join(formatted))
