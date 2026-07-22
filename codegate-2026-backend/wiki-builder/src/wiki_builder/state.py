from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from wiki_builder.config import WikiConfig, WikiScope, scoped_config
from wiki_builder.errors import ValidationError
from wiki_builder.io_utils import ensure_scoped_storage_path
from wiki_builder.schemas import validate_record


def load_current_build(
    config: WikiConfig,
    scope: WikiScope,
) -> tuple[dict[str, Any], Path] | None:
    """Load a validated current pointer and resolve it inside the wiki scope."""
    if not scope.current_pointer.is_file():
        return None
    try:
        pointer = json.loads(scope.current_pointer.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"current.json을 읽을 수 없습니다: {scope.current_pointer}") from exc
    if not isinstance(pointer, dict):
        raise ValidationError("current.json의 최상위 값은 객체여야 합니다")

    active_config = scoped_config(config, scope)
    validate_record(active_config, "current", pointer, "current.json")
    if pointer["tenant_id"] != scope.tenant_id or pointer["wiki_id"] != scope.wiki_id:
        raise ValidationError("current.json의 tenant_id 또는 wiki_id가 요청 범위와 다릅니다")
    expected_relative_path = f"builds/{pointer['build_id']}"
    if pointer["relative_path"] != expected_relative_path:
        raise ValidationError("current.json의 build_id와 relative_path가 서로 다릅니다")

    build_dir = scope.builds_dir / pointer["build_id"]
    ensure_scoped_storage_path(build_dir, scope.storage_root)
    return pointer, build_dir
