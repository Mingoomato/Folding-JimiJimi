from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from wiki_builder.builder import build_wiki, prepare_build_data
from wiki_builder.config import (
    WikiConfig,
    WikiScope,
    default_config_path,
    default_scope,
    load_config,
    scoped_config,
)
from wiki_builder.corpus import verify_expected_corpus
from wiki_builder.enrichment import enrich_documents
from wiki_builder.errors import WikiBuilderError
from wiki_builder.gemini import doctor
from wiki_builder.io_utils import CURRENT_NOT_CHECKED
from wiki_builder.markdown import load_documents
from wiki_builder.service import WikiBuildRequest, WikiBuildService
from wiki_builder.validation import validate_wiki


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wiki-builder",
        description="서버 연동형 Markdown Gemini LLM 위키 빌더",
    )
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--tenant-id", help="테넌트 식별자")
    parser.add_argument("--wiki-id", help="위키 식별자")
    parser.add_argument("--input-dir", type=Path, help="정규화 Markdown 입력 폴더")
    parser.add_argument("--storage-root", type=Path, help="불변 위키 빌드 저장소")
    parser.add_argument("--actor-id", default="local-user", help="감사 로그 행위자")
    parser.add_argument(
        "--production",
        action="store_true",
        help="실제 문서로 처리하여 외부 LLM 데이터 정책을 강제",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="Gemini API 키, 모델 접근 및 데이터 정책 점검")
    subparsers.add_parser("normalize", help="입력 계약과 본문 보존 조건 점검")
    build = subparsers.add_parser("build", help="새 불변 위키 빌드 생성")
    build.add_argument(
        "--activate-incomplete",
        action="store_true",
        help="미승인 enrichment가 있어도 current로 활성화(개발 전용)",
    )
    enrich = subparsers.add_parser("enrich", help="Gemini API enrichment 생성 후 빌드")
    enrich.add_argument("--ids", nargs="+", help="처리할 문서 ID 목록")
    enrich.add_argument("--refresh-enrichment", action="store_true")
    rebuild = subparsers.add_parser("rebuild", help="변경 문서 단위 enrichment와 재빌드")
    rebuild.add_argument("--ids", nargs="+", required=True)
    rebuild.add_argument("--without-enrichment", action="store_true")
    rebuild.add_argument("--refresh-enrichment", action="store_true")
    validate = subparsers.add_parser("validate", help="현재 활성 빌드 검증")
    validate.add_argument("--allow-pending", action="store_true")
    validate.add_argument("--skip-reproducibility", action="store_true")
    validate.add_argument("--expectations", type=Path, help="테스트 fixture 기대값 YAML")
    subparsers.add_parser("status", help="현재 활성 빌드 상태 조회")
    return parser


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _scope(config: WikiConfig, args: argparse.Namespace) -> WikiScope:
    base = default_scope(config)
    return WikiScope(
        tenant_id=args.tenant_id or base.tenant_id,
        wiki_id=args.wiki_id or base.wiki_id,
        input_dir=(args.input_dir or base.input_dir).resolve(),
        storage_root=(args.storage_root or base.storage_root).resolve(),
        synthetic_corpus=not args.production and base.synthetic_corpus,
        actor_id=args.actor_id,
    )


def _request(scope: WikiScope) -> WikiBuildRequest:
    return WikiBuildRequest(
        tenant_id=scope.tenant_id,
        wiki_id=scope.wiki_id,
        input_dir=scope.input_dir,
        storage_root=scope.storage_root,
        actor_id=scope.actor_id,
        synthetic_corpus=scope.synthetic_corpus,
        expected_current_build_id=CURRENT_NOT_CHECKED,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        scope = _scope(config, args)
        active_config = scoped_config(config, scope)
        service = WikiBuildService(config)
        if args.command == "doctor":
            report = doctor(config)
            report["scope"] = {
                "tenant_id": scope.tenant_id,
                "wiki_id": scope.wiki_id,
                "input_dir": str(scope.input_dir),
                "wiki_root": str(scope.wiki_root),
                "synthetic_corpus": scope.synthetic_corpus,
            }
            _print_json(report)
            return 0 if report["ok"] else 1
        if args.command == "normalize":
            documents = load_documents(active_config)
            _print_json(
                {
                    "ok": True,
                    "tenant_id": scope.tenant_id,
                    "wiki_id": scope.wiki_id,
                    "documents": len(documents),
                    "source_files": sum(len(document.source_fragments) for document in documents),
                    "source_fragment_documents": sum(
                        document.chunking_mode == "source-fragments" for document in documents
                    ),
                    "sections": sum(len(document.sections) for document in documents),
                    "input_dir": str(scope.input_dir),
                    "input_modified": False,
                }
            )
            return 0
        if args.command == "build":
            result = build_wiki(
                config,
                scope,
                activate_incomplete=args.activate_incomplete,
            )
            _print_json(
                {
                    "ok": True,
                    "build_id": result.build_id,
                    "build_dir": str(result.build_dir),
                    "created": result.created,
                    "activated": result.activated,
                    "documents": len(result.documents),
                    "source_fragments": sum(
                        len(document.source_fragments) for document in result.documents
                    ),
                    "chunks": len(result.chunks),
                    "aliases": len(result.aliases["entries"]),
                    "links": len(result.links),
                    "enrichments": len(result.enrichments),
                }
            )
            return 0
        if args.command == "enrich":
            run = enrich_documents(
                config,
                set(args.ids) if args.ids else None,
                args.refresh_enrichment,
                progress=lambda message: print(message, flush=True),
                scope=scope,
            )
            result = build_wiki(config, scope)
            _print_json(
                {
                    "ok": not run.failed,
                    "processed": run.processed,
                    "skipped": run.skipped,
                    "failed": run.failed,
                    "build_id": result.build_id,
                    "activated": result.activated,
                }
            )
            return 1 if run.failed else 0
        if args.command == "rebuild":
            result = service.rebuild_documents(
                _request(scope),
                set(args.ids),
                run_enrichment=not args.without_enrichment,
                refresh_enrichment=args.refresh_enrichment,
            )
            _print_json(
                {
                    "ok": result.enrichment is None or not result.enrichment.failed,
                    "build_id": result.build.build_id,
                    "activated": result.build.activated,
                    "enrichment": (
                        None
                        if result.enrichment is None
                        else {
                            "processed": result.enrichment.processed,
                            "skipped": result.enrichment.skipped,
                            "failed": result.enrichment.failed,
                        }
                    ),
                }
            )
            return 0
        if args.command == "validate":
            result = validate_wiki(
                config,
                require_enrichment=not args.allow_pending,
                check_reproducible=not args.skip_reproducibility,
                scope=scope,
            )
            if args.expectations:
                expectations = yaml.safe_load(args.expectations.read_text(encoding="utf-8"))
                verify_expected_corpus(expectations, prepare_build_data(active_config))
                result["expectations"] = "passed"
            _print_json(result)
            return 0
        if args.command == "status":
            _print_json(service.current_status(_request(scope)))
            return 0
        prepare_build_data(active_config)
        return 0
    except WikiBuilderError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
