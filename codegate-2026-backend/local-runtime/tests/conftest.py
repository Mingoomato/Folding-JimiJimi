from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from llm_wiki_local.config import RuntimeSettings
from llm_wiki_local.input_snapshots import InputSnapshots
from llm_wiki_local.models import BuildOutcome
from llm_wiki_local.runtime import LocalWikiRuntime
from llm_wiki_local.store import StateStore


class FakeConverter:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def convert(
        self,
        path: Path,
        metadata: dict[str, Any],
        *,
        job_id: str,
        progress=None,
    ) -> dict[str, Any]:
        self.calls.append({"path": path, "metadata": dict(metadata), "job_id": job_id})
        if progress is not None:
            progress("convert", "테스트 문서 변환 중")
        title = str(metadata.get("title") or "테스트 문서")
        body = f"# {title}\n\n## 본문\n\n{path.read_text(encoding='utf-8')}"
        frontmatter = {
            "schema_version": "1.0.0",
            "id": metadata["id"],
            "title": title,
            "doc_type": metadata.get("doc_type", "general"),
            "language": metadata.get("language", "ko"),
            "revision": str(metadata.get("revision", "1")),
            "status": metadata.get("status", "active"),
            "chunk_no": "converter-generated-name_001",
            "official_number": metadata.get("official_number"),
            "authority_level": metadata.get("authority_level"),
            "issuing_org": metadata.get("issuing_org"),
            "issued_on": metadata.get("issued_on"),
            "effective_from": metadata.get("effective_from"),
            "effective_to": metadata.get("effective_to"),
            "source": {
                "filename": path.name,
                "uri": metadata.get("uri", f"source://{path.name}"),
                "sha256": "0" * 64,
            },
            "access": metadata.get("access", "internal"),
            "tags": metadata.get("tags", []),
            "aliases": metadata.get("aliases", []),
            # Real doc2md responses include converter-only provenance. The local
            # runtime must not leak these into wiki-builder front matter.
            "canonical_sha256": "1" * 64,
            "converter_version": "1.1.0",
        }
        return {
            "markdown": body,
            "frontmatter": frontmatter,
            "body": body,
            "format": path.suffix.lstrip("."),
            "library_used": "fake",
            "warnings": [],
            "cached": False,
            "chunks": [
                {
                    "chunk_no": "converter-generated-name_001",
                    "index": 1,
                    "markdown": body,
                    "body": body,
                    "frontmatter": frontmatter,
                    "heading_path": [],
                    "chars": len(body),
                }
            ],
        }


class FakeBuilder:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self.activate = True

    def build(self, input_dir: Path, *, doc_id: str, deleted: bool) -> BuildOutcome:
        self.calls.append(
            {
                "doc_id": doc_id,
                "deleted": deleted,
                "files": {
                    path.name: path.read_text(encoding="utf-8")
                    for path in sorted(input_dir.glob("*.md"))
                },
            }
        )
        return BuildOutcome(
            build_id=f"build-test-{len(self.calls)}",
            activated=self.activate,
        )


@pytest.fixture
def settings(tmp_path: Path) -> RuntimeSettings:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    return RuntimeSettings(
        config_path=tmp_path / "wiki.yaml",
        state_dir=tmp_path / "local-data",
        bootstrap_input_dir=tmp_path / "bootstrap-input",
        storage_root=tmp_path / "wiki-storage",
        allowed_source_roots=(source_root,),
        run_enrichment=False,
        synthetic_corpus=True,
    )


@pytest.fixture
def runtime(settings: RuntimeSettings):
    converter = FakeConverter()
    builder = FakeBuilder()
    value = LocalWikiRuntime(
        settings,
        store=StateStore(settings.database_path),
        converter=converter,
        snapshots=InputSnapshots(
            settings.input_snapshots_dir,
            settings.bootstrap_input_dir,
        ),
        builder=builder,
    )
    value.fake_converter = converter
    value.fake_builder = builder
    try:
        yield value
    finally:
        value.shutdown()
