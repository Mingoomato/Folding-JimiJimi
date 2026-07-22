from __future__ import annotations

from wiki_builder.builder import build_wiki
from wiki_builder.config import WikiConfig, WikiScope
from wiki_builder.validation import validate_wiki


def test_current_nonrelease_output_is_structurally_valid(
    config: WikiConfig,
    tmp_path,
) -> None:
    scope = WikiScope(
        tenant_id="local",
        wiki_id="sample-wiki",
        input_dir=config.resolve_path("input_dir"),
        storage_root=tmp_path / "storage",
        synthetic_corpus=True,
        actor_id="pytest",
    )
    build = build_wiki(config, scope)
    assert build.activated is True
    result = validate_wiki(
        config,
        require_enrichment=True,
        check_reproducible=True,
        scope=scope,
        output_dir=build.build_dir,
    )
    assert result["documents"] == 50
    assert result["source_fragments"] == 50
    assert result["chunks"] == 148
    assert result["links"] == 38
    assert result["enrichments"] == 50
