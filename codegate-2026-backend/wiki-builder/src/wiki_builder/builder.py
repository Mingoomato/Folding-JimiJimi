from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from wiki_builder.config import WikiConfig, WikiScope, default_scope, scoped_config
from wiki_builder.corpus import assemble_build_data
from wiki_builder.io_utils import (
    CURRENT_NOT_CHECKED,
    activate_build,
    create_build_staging_dir,
    discard_staging_dir,
    install_immutable_build,
)
from wiki_builder.markdown import load_documents
from wiki_builder.models import BuildData
from wiki_builder.provenance import model_fingerprint
from wiki_builder.render import render_wiki
from wiki_builder.schemas import validate_record

BUILD_FORMAT_VERSION = "server-v2"


@dataclass(frozen=True)
class BuildResult:
    build_id: str
    build_dir: Path
    wiki_root: Path
    activated: bool
    created: bool
    data: BuildData

    @property
    def documents(self):
        return self.data.documents

    @property
    def chunks(self):
        return self.data.chunks

    @property
    def aliases(self):
        return self.data.aliases

    @property
    def links(self):
        return self.data.links

    @property
    def manifest(self):
        return self.data.manifest

    @property
    def enrichments(self):
        return self.data.enrichments


def prepare_build_data(config: WikiConfig) -> BuildData:
    documents = load_documents(config)
    return assemble_build_data(config, documents)


def compute_build_id(config: WikiConfig, data: BuildData) -> str:
    schema_digests = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(config.resolve_path("schemas_dir").glob("*.schema.json"))
    }
    renderer_path = config.project_dir / "src" / "wiki_builder" / "render.py"
    material = {
        "build_format_version": BUILD_FORMAT_VERSION,
        "renderer_sha256": hashlib.sha256(renderer_path.read_bytes()).hexdigest(),
        "schema_digests": schema_digests,
        "schema_version": config.schema_version,
        "wiki_version": config.wiki_version,
        "tenant_id": config.scope.get("tenant_id"),
        "wiki_id": config.scope.get("wiki_id"),
        "model_fingerprint": model_fingerprint(config),
        "manifest": data.manifest,
        "chunks": data.chunks,
        "aliases": data.aliases,
        "links": data.links,
        "enrichments": data.enrichments,
    }
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "build-" + hashlib.sha256(encoded).hexdigest()[:20]


def _usage_summary(data: BuildData) -> dict[str, Any]:
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    estimated_cost = 0.0
    measured_records = 0
    for enrichment in data.enrichments:
        usage = enrichment.get("generation", {}).get("usage")
        if not usage:
            continue
        measured_records += 1
        input_tokens += int(usage.get("input_tokens", 0))
        output_tokens += int(usage.get("output_tokens", 0))
        total_tokens += int(usage.get("total_tokens", 0))
        estimated_cost += float(usage.get("estimated_cost", 0.0))
    return {
        "measured_records": measured_records,
        "unmeasured_records": len(data.enrichments) - measured_records,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "estimated_cost": round(estimated_cost, 8),
        "currency": "USD",
    }


def make_build_meta(
    config: WikiConfig,
    scope: WikiScope,
    data: BuildData,
    build_id: str,
) -> dict[str, Any]:
    source_digest = hashlib.sha256(
        "\n".join(
            f"{document.doc_id}:{document.original_sha256}" for document in data.documents
        ).encode("utf-8")
    ).hexdigest()
    statuses: dict[str, int] = {}
    for record in data.manifest:
        status = record["enrichment"]["status"]
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "schema_version": config.schema_version,
        "wiki_version": config.wiki_version,
        "build_format_version": BUILD_FORMAT_VERSION,
        "tenant_id": scope.tenant_id,
        "wiki_id": scope.wiki_id,
        "build_id": build_id,
        "source_digest": source_digest,
        "model_fingerprint": model_fingerprint(config),
        "provider": config.provider["name"],
        "model": config.provider["model"],
        "counts": {
            "documents": len(data.documents),
            "source_fragments": sum(len(document.source_fragments) for document in data.documents),
            "chunks": len(data.chunks),
            "aliases": len(data.aliases["entries"]),
            "links": len(data.links),
            "enrichments": len(data.enrichments),
        },
        "enrichment_statuses": dict(sorted(statuses.items())),
        "usage": _usage_summary(data),
        "complete": all(record["enrichment"]["status"] == "approved" for record in data.manifest),
    }


def _config_with_build_id(config: WikiConfig, build_id: str) -> WikiConfig:
    raw = deepcopy(config.raw)
    raw["build_id"] = build_id
    return WikiConfig(path=config.path, raw=raw)


def _migrate_default_sample_cache(base_config: WikiConfig, scope: WikiScope) -> None:
    defaults = base_config.raw["defaults"]
    if (
        scope.tenant_id != str(defaults["tenant_id"])
        or scope.wiki_id != str(defaults["wiki_id"])
        or scope.input_dir.resolve() != base_config.resolve_path("input_dir")
    ):
        return
    legacy = base_config.resolve_path("enrichment_dir")
    target = scope.enrichment_dir
    if target.exists() or not legacy.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(legacy, target)


def _stage_wiki(
    config: WikiConfig,
    scope: WikiScope | None = None,
) -> tuple[BuildResult, bool]:
    selected_scope = scope or default_scope(config)
    _migrate_default_sample_cache(config, selected_scope)
    active_config = scoped_config(config, selected_scope)
    data = prepare_build_data(active_config)
    build_id = compute_build_id(active_config, data)
    render_config = _config_with_build_id(active_config, build_id)
    build_meta = make_build_meta(render_config, selected_scope, data, build_id)
    validate_record(render_config, "build-meta", build_meta, build_id)
    target = selected_scope.builds_dir / build_id
    staging = create_build_staging_dir(selected_scope.builds_dir, selected_scope.storage_root)
    try:
        render_wiki(render_config, data, staging, build_meta)
        created = install_immutable_build(staging, target, selected_scope.storage_root)
    except BaseException:
        discard_staging_dir(staging)
        raise
    return (
        BuildResult(
            build_id=build_id,
            build_dir=target,
            wiki_root=selected_scope.wiki_root,
            activated=False,
            created=created,
            data=data,
        ),
        bool(build_meta["complete"]),
    )


def stage_wiki(
    config: WikiConfig,
    scope: WikiScope | None = None,
) -> BuildResult:
    result, _ = _stage_wiki(config, scope)
    return result


def _activate_staged_build(
    config: WikiConfig,
    scope: WikiScope,
    *,
    build_id: str,
    complete: bool,
    expected_current_build_id: str | None | object,
    before_write: Callable[[], None] | None = None,
) -> None:
    active_config = scoped_config(config, scope)
    render_config = _config_with_build_id(active_config, build_id)
    pointer = {
        "schema_version": config.schema_version,
        "tenant_id": scope.tenant_id,
        "wiki_id": scope.wiki_id,
        "build_id": build_id,
        "relative_path": f"builds/{build_id}",
        "complete": complete,
    }
    validate_record(render_config, "current", pointer, "current.json")
    activate_build(
        scope.wiki_root,
        scope.storage_root,
        pointer,
        scope.actor_id,
        expected_current_build_id,
        before_write,
    )


def build_wiki(
    config: WikiConfig,
    scope: WikiScope | None = None,
    *,
    activate_incomplete: bool = False,
    expected_current_build_id: str | None | object = CURRENT_NOT_CHECKED,
) -> BuildResult:
    selected_scope = scope or default_scope(config)
    result, complete = _stage_wiki(config, selected_scope)
    should_activate = complete or activate_incomplete
    if should_activate:
        _activate_staged_build(
            config,
            selected_scope,
            build_id=result.build_id,
            complete=complete,
            expected_current_build_id=expected_current_build_id,
        )
    return replace(result, activated=should_activate)


def render_to_temporary(
    config: WikiConfig,
    parent: Path,
    scope: WikiScope | None = None,
) -> tuple[BuildData, Path]:
    selected_scope = scope or default_scope(config)
    active_config = scoped_config(config, selected_scope)
    data = prepare_build_data(active_config)
    build_id = compute_build_id(active_config, data)
    render_config = _config_with_build_id(active_config, build_id)
    destination = parent / build_id
    render_wiki(
        render_config,
        data,
        destination,
        make_build_meta(render_config, selected_scope, data, build_id),
    )
    return data, destination
