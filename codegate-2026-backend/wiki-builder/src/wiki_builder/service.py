from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wiki_builder.builder import BuildResult, _activate_staged_build, build_wiki, stage_wiki
from wiki_builder.config import WikiConfig, WikiScope, scoped_config
from wiki_builder.enrichment import EnrichmentRun, enrich_documents
from wiki_builder.errors import UnsafeOutputError, ValidationError
from wiki_builder.gemini import get_api_key
from wiki_builder.io_utils import CURRENT_NOT_CHECKED, ensure_scoped_storage_path
from wiki_builder.schemas import validate_record
from wiki_builder.state import load_current_build
from wiki_builder.validation import validate_wiki


@dataclass(frozen=True)
class WikiBuildRequest:
    tenant_id: str
    wiki_id: str
    input_dir: Path
    storage_root: Path
    actor_id: str
    synthetic_corpus: bool = False
    expected_current_build_id: str | None | object = CURRENT_NOT_CHECKED

    def to_scope(self) -> WikiScope:
        return WikiScope(
            tenant_id=self.tenant_id,
            wiki_id=self.wiki_id,
            input_dir=self.input_dir,
            storage_root=self.storage_root,
            synthetic_corpus=self.synthetic_corpus,
            actor_id=self.actor_id,
        )


@dataclass(frozen=True)
class RebuildResult:
    enrichment: EnrichmentRun | None
    build: BuildResult


class WikiBuildService:
    """Server-facing boundary for one tenant-scoped wiki."""

    def __init__(self, config: WikiConfig):
        self.config = config

    def build(
        self,
        request: WikiBuildRequest,
        *,
        activate_incomplete: bool = False,
    ) -> BuildResult:
        return build_wiki(
            self.config,
            request.to_scope(),
            activate_incomplete=activate_incomplete,
            expected_current_build_id=request.expected_current_build_id,
        )

    def stage(self, request: WikiBuildRequest) -> BuildResult:
        return stage_wiki(self.config, request.to_scope())

    def enrich_documents_if_configured(
        self,
        request: WikiBuildRequest,
        *,
        doc_ids: set[str] | None = None,
        refresh: bool = False,
    ) -> EnrichmentRun | None:
        """Refresh stale enrichment only when the operator supplied a Gemini key.

        A missing key deliberately leaves the subsequent candidate incomplete. When a
        key exists, ``enrich_documents`` owns cache validation, privacy-policy checks,
        provider calls, and approved-record writes.
        """
        if get_api_key(self.config, required=False) is None:
            return None
        run = enrich_documents(
            self.config,
            selected_ids=doc_ids,
            refresh=refresh,
            scope=request.to_scope(),
        )
        if run.failed:
            failed_ids = ", ".join(run.failed[:10])
            remainder = len(run.failed) - 10
            if remainder > 0:
                failed_ids = f"{failed_ids} 외 {remainder}개"
            raise ValidationError(
                f"Gemini enrichment 검증에 실패한 문서가 있어 후보 빌드를 중단합니다: {failed_ids}"
            )
        return run

    def validate_candidate(
        self,
        request: WikiBuildRequest,
        build_id: str,
        *,
        require_enrichment: bool = True,
        check_reproducible: bool = True,
    ) -> dict[str, Any]:
        scope = request.to_scope()
        build_dir = self._candidate_dir(scope, build_id)
        return validate_wiki(
            self.config,
            require_enrichment=require_enrichment,
            check_reproducible=check_reproducible,
            scope=scope,
            output_dir=build_dir,
        )

    def activate_candidate(
        self,
        request: WikiBuildRequest,
        build_id: str,
        *,
        activate_incomplete: bool = False,
        check_reproducible: bool = True,
    ) -> dict[str, Any]:
        if request.expected_current_build_id is CURRENT_NOT_CHECKED:
            raise ValidationError("후보 활성화에는 expected_current_build_id가 필요합니다")
        scope = request.to_scope()
        build_dir = self._candidate_dir(scope, build_id)
        try:
            build_meta = json.loads((build_dir / "build-meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError("후보 build-meta.json을 읽을 수 없습니다") from exc
        active_config = scoped_config(self.config, scope)
        validate_record(active_config, "build-meta", build_meta, build_id)
        complete = bool(build_meta["complete"])
        if not complete and not activate_incomplete:
            raise ValidationError("불완전한 후보 빌드는 활성화할 수 없습니다")
        validation: dict[str, Any] = {}

        def validate_before_write() -> None:
            checked = self.validate_candidate(
                request,
                build_id,
                require_enrichment=not activate_incomplete,
                check_reproducible=check_reproducible,
            )
            if bool(checked["complete"]) != complete:
                raise ValidationError("후보 빌드 완전성 상태가 검증 중 변경됐습니다")
            validation.update(checked)

        _activate_staged_build(
            self.config,
            scope,
            build_id=build_id,
            complete=complete,
            expected_current_build_id=request.expected_current_build_id,
            before_write=validate_before_write,
        )
        return {**validation, "activated": True, "complete": complete}

    def rebuild_documents(
        self,
        request: WikiBuildRequest,
        doc_ids: set[str],
        *,
        run_enrichment: bool = True,
        refresh_enrichment: bool = False,
    ) -> RebuildResult:
        if not doc_ids:
            raise ValidationError("재빌드할 doc_ids가 비어 있습니다")
        scope = request.to_scope()
        enrichment: EnrichmentRun | None = None
        if run_enrichment:
            enrichment = enrich_documents(
                self.config,
                selected_ids=doc_ids,
                refresh=refresh_enrichment,
                scope=scope,
            )
        build = build_wiki(
            self.config,
            scope,
            expected_current_build_id=request.expected_current_build_id,
        )
        return RebuildResult(enrichment=enrichment, build=build)

    def current_status(self, request: WikiBuildRequest) -> dict[str, Any]:
        scope = request.to_scope()
        current_build = load_current_build(self.config, scope)
        if current_build is None:
            return {
                "tenant_id": scope.tenant_id,
                "wiki_id": scope.wiki_id,
                "status": "not-built",
                "current": None,
            }
        current, build_dir = current_build
        meta_path = build_dir / "build-meta.json"
        if not meta_path.is_file():
            raise ValidationError(f"current가 가리키는 build-meta.json이 없습니다: {build_dir}")
        build_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        validate_record(self.config, "build-meta", build_meta, current["build_id"])
        if build_meta["build_id"] != current["build_id"]:
            raise ValidationError("current.json과 build-meta.json의 build_id가 다릅니다")
        return {
            "tenant_id": scope.tenant_id,
            "wiki_id": scope.wiki_id,
            "status": "ready" if current.get("complete") else "incomplete",
            "current": current,
            "build_meta": build_meta,
        }

    def validate_current(
        self,
        request: WikiBuildRequest,
        *,
        require_enrichment: bool = True,
        check_reproducible: bool = True,
    ) -> dict[str, Any]:
        scope = request.to_scope()
        current_build = load_current_build(self.config, scope)
        if current_build is None:
            raise ValidationError("현재 활성화된 빌드가 없습니다")
        _, build_dir = current_build
        return validate_wiki(
            self.config,
            require_enrichment=require_enrichment,
            check_reproducible=check_reproducible,
            scope=scope,
            output_dir=build_dir,
        )

    @staticmethod
    def _candidate_dir(scope: WikiScope, build_id: str) -> Path:
        if re.fullmatch(r"build-[0-9a-f]{20}", build_id) is None:
            raise ValidationError(f"잘못된 build_id: {build_id!r}")
        build_dir = scope.builds_dir / build_id
        try:
            ensure_scoped_storage_path(build_dir, scope.storage_root)
        except UnsafeOutputError as exc:
            raise ValidationError(f"안전하지 않은 후보 빌드 경로입니다: {build_id}") from exc
        if build_dir.is_symlink() or not build_dir.is_dir():
            raise ValidationError(f"후보 빌드 디렉터리가 없습니다: {build_id}")
        return build_dir
