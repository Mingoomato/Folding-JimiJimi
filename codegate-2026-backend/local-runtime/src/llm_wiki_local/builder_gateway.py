from __future__ import annotations

from pathlib import Path

from wiki_builder.config import load_config
from wiki_builder.service import WikiBuildRequest, WikiBuildService

from llm_wiki_local.models import BuildOutcome


class WikiBuilderGateway:
    def __init__(
        self,
        *,
        config_path: Path,
        storage_root: Path,
        tenant_id: str,
        wiki_id: str,
        actor_id: str,
        synthetic_corpus: bool,
        run_enrichment: bool,
    ):
        self.config = load_config(config_path)
        self.service = WikiBuildService(self.config)
        self.storage_root = storage_root
        self.tenant_id = tenant_id
        self.wiki_id = wiki_id
        self.actor_id = actor_id
        self.synthetic_corpus = synthetic_corpus
        self.run_enrichment = run_enrichment

    def build(
        self,
        input_dir: Path,
        *,
        doc_id: str,
        deleted: bool,
    ) -> BuildOutcome:
        probe = self._request(input_dir, expected_current_build_id=None)
        status = self.service.current_status(probe)
        current = status.get("current")
        expected = current.get("build_id") if isinstance(current, dict) else None
        request = self._request(input_dir, expected_current_build_id=expected)
        if deleted:
            result = self.service.build(request)
        else:
            rebuild = self.service.rebuild_documents(
                request,
                {doc_id},
                run_enrichment=self.run_enrichment,
            )
            result = rebuild.build
        return BuildOutcome(build_id=result.build_id, activated=result.activated)

    def _request(
        self,
        input_dir: Path,
        *,
        expected_current_build_id: str | None,
    ) -> WikiBuildRequest:
        return WikiBuildRequest(
            tenant_id=self.tenant_id,
            wiki_id=self.wiki_id,
            input_dir=input_dir,
            storage_root=self.storage_root,
            actor_id=self.actor_id,
            synthetic_corpus=self.synthetic_corpus,
            expected_current_build_id=expected_current_build_id,
        )
