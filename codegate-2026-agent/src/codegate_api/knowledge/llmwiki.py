from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import mimetypes
import os
import re
import shutil
import sys
import threading
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from codegate_api.files.atomic import DocumentLockManager, SafeFileError, SafeSourceFileStore
from codegate_api.files.resolver import SourceUriResolver
from codegate_api.integrations.doc2md import Doc2MdClient, Doc2MdError
from codegate_api.knowledge.access import AccessContext
from codegate_api.knowledge.pipeline import (
    ContentChangedPayload,
    SyncPipelineError,
    _deterministic_embedding,
    _json,
    _write_checksums,
    _write_jsonl,
)
from codegate_api.knowledge.repository import KnowledgeRepository
from codegate_api.knowledge.schemas import (
    AccessLevel,
    AgentGuide,
    Chunk,
    Link,
    ManifestEntry,
    SourceReference,
)
from codegate_api.state.store import PendingEvent, StateStore

_STANDALONE_NATIVE_ANCHOR = re.compile(
    r'^\s*<a\s+id=["\'](?P<id>[0-9A-Za-z][0-9A-Za-z._:-]{0,127})["\']\s*></a>\s*$'
)


class LLMWikiCompatibilityError(SyncPipelineError):
    def __init__(self, message: str) -> None:
        super().__init__("LLMWIKI_SYNC_FAILED", message)


@dataclass(frozen=True, slots=True)
class NativeBuild:
    build_id: str
    build_dir: Path
    complete: bool
    manifest_by_id: dict[str, dict[str, Any]]
    section_ids_by_document: dict[str, frozenset[str]]


@dataclass(frozen=True, slots=True)
class NativeInputBaseline:
    path: Path
    file_sha256: str
    text: str


@dataclass(frozen=True, slots=True)
class NativeInputFragment:
    chunk_no: str | None
    relative_path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class NativeIngest:
    chunking_mode: str
    fragments: tuple[NativeInputFragment, ...]
    descriptor_sha256: str

    @property
    def supports_atomic_edit(self) -> bool:
        return (
            self.chunking_mode == "sections"
            and len(self.fragments) == 1
            and self.fragments[0].chunk_no is None
        )


@dataclass(frozen=True, slots=True)
class PreparedNativeBuild:
    build: NativeBuild
    compatibility_root: Path
    repository: KnowledgeRepository
    native_digest: str


class InProcessLLMWikiBuildRunner:
    """Call the trusted bundled WikiBuildService without exposing a shell tool."""

    def __init__(
        self,
        *,
        project_root: Path,
        input_root: Path,
        storage_root: Path,
        tenant_id: str,
        wiki_id: str,
        actor_id: str,
    ) -> None:
        self._builder_root = _builder_root(project_root)
        self._input_root = input_root.resolve()
        self._storage_root = storage_root.resolve()
        self._tenant_id = tenant_id
        self._wiki_id = wiki_id
        self._actor_id = actor_id
        self._lock = threading.Lock()

    def stage(self) -> str:
        with self._lock:
            config_module, service_module = self._load_modules()
            config = config_module.load_config(self._builder_root / "config/wiki.yaml")
            request = self._request(service_module, expected_current_build_id=None)
            service = service_module.WikiBuildService(config)
            try:
                enrichment = service.enrich_documents_if_configured(request, refresh=False)
                if enrichment is not None and enrichment.failed:
                    failed_ids = ", ".join(enrichment.failed[:10])
                    raise LLMWikiCompatibilityError(
                        f"LLMWIKI enrichment returned failed documents: {failed_ids}"
                    )
                result = service.stage(request)
            except Exception as error:
                raise LLMWikiCompatibilityError(
                    f"LLMWIKI stage failed: {type(error).__name__}: {error}"
                ) from error
            if result.activated:
                raise LLMWikiCompatibilityError("LLMWIKI stage unexpectedly activated a build")
            return str(result.build_id)

    def activate(self, *, build_id: str, expected_current_build_id: str | None) -> None:
        with self._lock:
            config_module, service_module = self._load_modules()
            config = config_module.load_config(self._builder_root / "config/wiki.yaml")
            request = self._request(
                service_module,
                expected_current_build_id=expected_current_build_id,
            )
            service = service_module.WikiBuildService(config)
            try:
                service.activate_candidate(
                    request,
                    build_id,
                    activate_incomplete=True,
                )
            except Exception as error:
                raise LLMWikiCompatibilityError(
                    f"LLMWIKI activation failed: {type(error).__name__}: {error}"
                ) from error

    def _request(
        self,
        service_module: ModuleType,
        *,
        expected_current_build_id: str | None,
    ) -> Any:
        return service_module.WikiBuildRequest(
            tenant_id=self._tenant_id,
            wiki_id=self._wiki_id,
            input_dir=self._input_root,
            storage_root=self._storage_root,
            actor_id=self._actor_id,
            synthetic_corpus=False,
            expected_current_build_id=expected_current_build_id,
        )

    def _load_modules(self) -> tuple[ModuleType, ModuleType]:
        source_root = self._builder_root / "src"
        config_path = self._builder_root / "config/wiki.yaml"
        if not source_root.is_dir() or not config_path.is_file():
            raise LLMWikiCompatibilityError(f"LLMWIKI bundle is incomplete: {self._builder_root}")
        source_text = str(source_root)
        if source_text not in sys.path:
            sys.path.insert(0, source_text)
            importlib.invalidate_caches()
        try:
            return (
                importlib.import_module("wiki_builder.config"),
                importlib.import_module("wiki_builder.service"),
            )
        except ImportError as error:
            raise LLMWikiCompatibilityError(
                "LLMWIKI Python dependencies are not installed in the local app runtime"
            ) from error


class LLMWikiCompatibilityAdapter:
    """Map one immutable native LLMWIKI build into a checksummed Backend read cache."""

    def __init__(
        self,
        *,
        source_resolver: SourceUriResolver,
        input_root: Path,
        storage_root: Path,
        compatibility_root: Path,
        tenant_id: str,
        wiki_id: str,
    ) -> None:
        self._source_resolver = source_resolver
        self._input_root = input_root.resolve()
        self._storage_root = storage_root.resolve()
        self._compatibility_root = compatibility_root.resolve()
        self._tenant_id = tenant_id
        self._wiki_id = wiki_id
        self._lock = threading.RLock()

    @property
    def wiki_root(self) -> Path:
        return self._storage_root / "tenants" / self._tenant_id / "wikis" / self._wiki_id

    def current_native_build(self) -> NativeBuild | None:
        pointer_path = self.wiki_root / "current.json"
        if not pointer_path.is_file():
            return None
        if pointer_path.is_symlink():
            raise LLMWikiCompatibilityError("LLMWIKI current.json must not be a symlink")
        pointer = _read_json_object(pointer_path)
        expected_keys = {
            "schema_version",
            "tenant_id",
            "wiki_id",
            "build_id",
            "relative_path",
            "complete",
        }
        if set(pointer) != expected_keys:
            raise LLMWikiCompatibilityError("LLMWIKI current.json fields are invalid")
        build_id = pointer.get("build_id")
        if (
            pointer.get("schema_version") != "1.0.0"
            or pointer.get("tenant_id") != self._tenant_id
            or pointer.get("wiki_id") != self._wiki_id
            or not isinstance(build_id, str)
            or not re.fullmatch(r"build-[0-9a-f]{20}", build_id)
            or pointer.get("relative_path") != f"builds/{build_id}"
            or not isinstance(pointer.get("complete"), bool)
        ):
            raise LLMWikiCompatibilityError("LLMWIKI current.json is invalid")
        build = self.native_build(build_id)
        _raise_schema_errors(
            _native_validator(build.build_dir, "current"),
            pointer,
            "current.json",
        )
        if pointer["complete"] != build.complete:
            raise LLMWikiCompatibilityError("LLMWIKI current completeness does not match build")
        return build

    def native_build(self, build_id: str) -> NativeBuild:
        if re.fullmatch(r"build-[0-9a-f]{20}", build_id) is None:
            raise LLMWikiCompatibilityError("LLMWIKI build ID is invalid")
        build_dir = _safe_directory_child(self.wiki_root, f"builds/{build_id}")
        meta = _read_json_object(_safe_child(build_dir, "build-meta.json"))
        _raise_schema_errors(
            _native_validator(build_dir, "build-meta"),
            meta,
            "build-meta.json",
        )
        if (
            meta.get("build_id") != build_id
            or meta.get("tenant_id") != self._tenant_id
            or meta.get("wiki_id") != self._wiki_id
            or meta.get("build_format_version") not in {"server-v1", "server-v2"}
        ):
            raise LLMWikiCompatibilityError("LLMWIKI build metadata does not match current.json")
        manifests = self._read_validated_jsonl(build_dir, "manifest")
        manifest_by_id = {str(item.get("doc_id")): item for item in manifests}
        if len(manifest_by_id) != len(manifests) or "None" in manifest_by_id:
            raise LLMWikiCompatibilityError("LLMWIKI manifest document IDs are invalid")
        section_ids_by_document: dict[str, set[str]] = defaultdict(set)
        for chunk in self._read_validated_jsonl(build_dir, "chunk"):
            document_id = str(chunk.get("doc_id"))
            section_id = str(chunk.get("section_id"))
            if document_id not in manifest_by_id:
                raise LLMWikiCompatibilityError("LLMWIKI chunk references an unknown document")
            section_ids_by_document[document_id].add(section_id)
        return NativeBuild(
            build_id=build_id,
            build_dir=build_dir,
            complete=bool(meta["complete"]),
            manifest_by_id=manifest_by_id,
            section_ids_by_document={
                document_id: frozenset(section_ids)
                for document_id, section_ids in section_ids_by_document.items()
            },
        )

    def materialize(self, build: NativeBuild) -> Path:
        destination = self._compatibility_root / f"{build.build_id}-codegate-v3"
        with self._lock:
            if destination.is_dir():
                return destination
            self._compatibility_root.mkdir(parents=True, exist_ok=True)
            staging = self._compatibility_root / f".staging-{uuid4().hex}"
            try:
                self._render(build, staging)
                repository = KnowledgeRepository(staging, self._source_resolver)
                repository.load(validate_sources=False)
                _fsync_tree(staging)
                os.replace(staging, destination)
                _fsync_directory(self._compatibility_root)
            except Exception:
                _discard_tree(staging)
                raise
            return destination

    def input_path(self, build: NativeBuild, document_id: str) -> Path:
        manifest = build.manifest_by_id.get(document_id)
        if manifest is None:
            raise LLMWikiCompatibilityError(f"unknown LLMWIKI document: {document_id}")
        ingest = _native_ingest(manifest, document_id)
        if not ingest.supports_atomic_edit:
            raise LLMWikiCompatibilityError(
                f"LLMWIKI source-fragment document is read-only: {document_id}"
            )
        relative = Path(ingest.fragments[0].relative_path.removeprefix("source-md/"))
        return _safe_child(self._input_root, relative.as_posix())

    def input_baseline(self, build: NativeBuild, document_id: str) -> NativeInputBaseline:
        manifest = build.manifest_by_id.get(document_id)
        if manifest is None:
            raise LLMWikiCompatibilityError(f"unknown LLMWIKI document: {document_id}")
        ingest = _native_ingest(manifest, document_id)
        if not ingest.supports_atomic_edit:
            raise LLMWikiCompatibilityError(
                f"LLMWIKI source-fragment document is read-only: {document_id}"
            )
        fragment = ingest.fragments[0]
        canonical_path = _safe_child(build.build_dir, str(manifest["path"]))
        canonical_text = canonical_path.read_text(encoding="utf-8")
        baseline_text = _rewrite_native_anchors(
            canonical_text,
            build.section_ids_by_document.get(document_id, frozenset()),
            document_id=document_id,
            keep_section_anchors=False,
        )
        return NativeInputBaseline(
            path=self.input_path(build, document_id),
            file_sha256=fragment.sha256,
            text=baseline_text,
        )

    def _render(self, build: NativeBuild, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=False)
        native_manifests = list(build.manifest_by_id.values())
        native_chunks = self._read_validated_jsonl(build.build_dir, "chunk")
        native_links = self._read_validated_jsonl(build.build_dir, "link")
        native_aliases = self._read_validated_json(build.build_dir, "aliases")
        file_versions: dict[str, str] = {}
        manifests: list[dict[str, Any]] = []

        for native in native_manifests:
            document_id = str(native["doc_id"])
            ingest = _native_ingest(native, document_id)
            source = dict(native["source"])
            source_uri = str(source["uri"])
            source_sha256 = str(source["sha256"]).lower()
            source_path = self._source_resolver.resolve(source_uri)
            media_type = _media_type(str(source["filename"]))
            editable = media_type in {"text/markdown", "text/plain"} and ingest.supports_atomic_edit
            file_version_id = f"fv_{source_sha256}"
            file_versions[document_id] = file_version_id
            manifests.append(
                {
                    "schema_version": "1.0.0",
                    "id": document_id,
                    "file_version_id": file_version_id,
                    "title": native["title"],
                    "doc_type": native["doc_type"],
                    "language": native["language"],
                    "revision": str(native["revision"]),
                    "status": native["status"],
                    "official_number": native.get("official_number"),
                    "authority_level": native["authority_level"],
                    "issuing_org": native.get("issuing_org"),
                    "issued_on": native.get("issued_on"),
                    "effective_from": native.get("effective_from"),
                    "effective_to": native.get("effective_to"),
                    "source": {
                        "filename": source["filename"],
                        "uri": source_uri,
                        "media_type": media_type,
                        "sha256": source_sha256,
                    },
                    "canonical_path": native["path"],
                    "access": native["access"],
                    "write_access": "restricted" if editable else "none",
                    "editability": "editable" if editable else "read_only",
                    "tags": native["tags"],
                    "aliases": native["aliases"],
                    "conversion": {
                        "converter": "llm-wiki-builder",
                        "conversion_version": str(
                            _read_json_object(build.build_dir / "build-meta.json").get(
                                "build_format_version", "unknown"
                            )
                        ),
                        "converted_at": None,
                        "status": "succeeded",
                    },
                }
            )
            if not source_path.is_file():
                raise LLMWikiCompatibilityError(f"source file is missing: {source_uri}")
            canonical_source = _safe_child(build.build_dir, str(native["path"]))
            canonical_target = _safe_output_child(destination, str(native["path"]))
            canonical_target.parent.mkdir(parents=True, exist_ok=True)
            canonical_target.write_text(
                _rewrite_native_anchors(
                    canonical_source.read_text(encoding="utf-8"),
                    build.section_ids_by_document.get(document_id, frozenset()),
                    document_id=document_id,
                    keep_section_anchors=True,
                ),
                encoding="utf-8",
            )
            reference = {
                "schema_version": "1.0.0",
                "kind": "source_reference",
                "document_id": document_id,
                "source_uri": source_uri,
                "source_sha256": source_sha256,
            }
            reference_path = destination / f"references/{document_id}.source.json"
            reference_path.parent.mkdir(parents=True, exist_ok=True)
            reference_path.write_text(_json(reference) + "\n", encoding="utf-8")

        chunks, chunks_by_section = _convert_chunks(
            native_chunks,
            manifests={item["id"]: item for item in manifests},
            file_versions=file_versions,
        )
        links = _convert_links(native_links, chunks_by_section)
        aliases = _convert_aliases(native_aliases, manifests)

        (destination / "README.md").write_bytes(
            _safe_child(build.build_dir, "README.md").read_bytes()
        )
        (destination / "AGENT_GUIDE.md").write_text(_agent_guide(), encoding="utf-8")
        (destination / "VERSION").write_text(f"{build.build_id}\n", encoding="utf-8")
        _write_jsonl(destination / "manifest.jsonl", manifests)
        (destination / "retrieval").mkdir(parents=True, exist_ok=True)
        _write_jsonl(destination / "retrieval/chunks.jsonl", chunks)
        (destination / "retrieval/aliases.json").write_text(
            json.dumps(aliases, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _write_jsonl(destination / "retrieval/links.jsonl", links)
        _write_compatibility_schemas(destination)
        _write_local_indexes(destination, build.build_id, chunks, links)
        _write_checksums(destination)

    def _read_validated_jsonl(self, build_dir: Path, schema_name: str) -> list[dict[str, Any]]:
        filename = {
            "manifest": "manifest.jsonl",
            "chunk": "retrieval/chunks.jsonl",
            "link": "retrieval/links.jsonl",
        }[schema_name]
        records = _read_jsonl(_safe_child(build_dir, filename))
        validator = _native_validator(build_dir, schema_name)
        for index, record in enumerate(records, start=1):
            _raise_schema_errors(validator, record, f"{filename}:{index}")
        return records

    def _read_validated_json(self, build_dir: Path, schema_name: str) -> dict[str, Any]:
        filename = "retrieval/aliases.json"
        value = _read_json_object(_safe_child(build_dir, filename))
        _raise_schema_errors(_native_validator(build_dir, schema_name), value, filename)
        return value


class NativeLLMWikiCatalog:
    """Expose a pinned Backend repository while native current.json remains authoritative."""

    def __init__(
        self,
        *,
        adapter: LLMWikiCompatibilityAdapter,
        runner: InProcessLLMWikiBuildRunner,
        source_resolver: SourceUriResolver,
        state_store: StateStore,
    ) -> None:
        self._adapter = adapter
        self._runner = runner
        self._source_resolver = source_resolver
        self._state = state_store
        self._repository: KnowledgeRepository | None = None
        self._native_build: NativeBuild | None = None
        self._lock = threading.RLock()

    def bootstrap(
        self,
        *,
        allow_stale_sources: bool = False,
        replace_invalid_demo_seed: bool = False,
        allow_seed_bootstrap: bool = True,
    ) -> None:
        del replace_invalid_demo_seed, allow_seed_bootstrap
        with self._lock:
            build = self._adapter.current_native_build()
            if build is None:
                build_id = self._runner.stage()
                prepared = self._prepare(
                    self._adapter.native_build(build_id),
                    validate_sources=not allow_stale_sources,
                )
                self._record_digest(prepared, parent_build_id=None, status="validated")
                try:
                    self._runner.activate(build_id=build_id, expected_current_build_id=None)
                except LLMWikiCompatibilityError:
                    current = self._adapter.current_native_build()
                    if current is None:
                        raise
                    self._load(current, validate_sources=not allow_stale_sources)
                    return
                current = self._adapter.current_native_build()
                if current is None or current.build_id != build_id:
                    raise LLMWikiCompatibilityError("LLMWIKI bootstrap activation did not persist")
                if _native_build_digest(current) != prepared.native_digest:
                    raise LLMWikiCompatibilityError(
                        "bootstrapped LLMWIKI build differs from validation"
                    )
                self._promote(current, prepared.repository)
                self._record_digest(prepared, parent_build_id=None, status="active")
                return
            self._load(build, validate_sources=not allow_stale_sources)

    def snapshot(self) -> KnowledgeRepository:
        with self._lock:
            if self._repository is None:
                raise LLMWikiCompatibilityError("LLMWIKI catalog is not bootstrapped")
            return self._repository

    def refresh(self, *, allow_stale_sources: bool = False) -> KnowledgeRepository:
        with self._lock:
            build = self._adapter.current_native_build()
            if build is None:
                raise LLMWikiCompatibilityError("LLMWIKI current build is missing")
            self._load(build, validate_sources=not allow_stale_sources)
            return self.snapshot()

    def reset_to_seed(self) -> str:
        raise LLMWikiCompatibilityError("native LLMWIKI mode does not support demo reset")

    def current_native_build(self) -> NativeBuild:
        with self._lock:
            if self._native_build is None:
                raise LLMWikiCompatibilityError("LLMWIKI catalog is not bootstrapped")
            return self._native_build

    def input_path(self, document_id: str) -> Path:
        return self._adapter.input_path(self.current_native_build(), document_id)

    def input_baseline(self, document_id: str) -> NativeInputBaseline:
        with self._lock:
            if self._native_build is None:
                raise LLMWikiCompatibilityError("LLMWIKI catalog is not bootstrapped")
            return self._adapter.input_baseline(self._native_build, document_id)

    def stage(self) -> str:
        return self._runner.stage()

    def prepare_candidate(
        self,
        build_id: str,
        *,
        expected_current_build_id: str,
        changed_document_ids: set[str],
    ) -> PreparedNativeBuild:
        with self._lock:
            active = self.current_native_build()
            if active.build_id != expected_current_build_id:
                raise LLMWikiCompatibilityError("active LLMWIKI build changed before validation")
        candidate = self._adapter.native_build(build_id)
        if set(candidate.manifest_by_id) != set(active.manifest_by_id):
            raise LLMWikiCompatibilityError("LLMWIKI candidate document set changed")
        if not changed_document_ids <= set(candidate.manifest_by_id):
            raise LLMWikiCompatibilityError("LLMWIKI candidate lacks a changed document")
        for document_id, candidate_manifest in candidate.manifest_by_id.items():
            candidate_ingest = _native_ingest(candidate_manifest, document_id)
            active_ingest = _native_ingest(active.manifest_by_id[document_id], document_id)
            if document_id in changed_document_ids:
                if (
                    not active_ingest.supports_atomic_edit
                    or not candidate_ingest.supports_atomic_edit
                    or candidate_ingest.fragments[0].relative_path
                    != active_ingest.fragments[0].relative_path
                    or not self._state.is_known_llmwiki_input_state(
                        document_id=document_id,
                        active_build_id=expected_current_build_id,
                        content_sha256=candidate_ingest.fragments[0].sha256,
                    )
                ):
                    raise LLMWikiCompatibilityError(
                        f"LLMWIKI candidate contains an unapproved input: {document_id}"
                    )
            elif candidate_ingest.descriptor_sha256 != active_ingest.descriptor_sha256:
                raise LLMWikiCompatibilityError(
                    f"LLMWIKI candidate changed an unrelated input: {document_id}"
                )
        prepared = self._prepare(candidate)
        with self._lock:
            if self.current_native_build().build_id != expected_current_build_id:
                raise LLMWikiCompatibilityError("active LLMWIKI build changed during validation")
        self._record_digest(
            prepared,
            parent_build_id=expected_current_build_id,
            status="validated",
        )
        return prepared

    def activate_candidate(
        self,
        prepared: PreparedNativeBuild,
        *,
        expected_current_build_id: str,
    ) -> KnowledgeRepository:
        self._verify_persisted_digest(prepared)
        with self._lock:
            active = self.current_native_build()
            if active.build_id != expected_current_build_id:
                raise LLMWikiCompatibilityError("active LLMWIKI build changed before activation")
        prepared.repository.load(validate_sources=True)
        if _native_build_digest(prepared.build) != prepared.native_digest:
            raise LLMWikiCompatibilityError("LLMWIKI candidate changed after validation")
        try:
            self._runner.activate(
                build_id=prepared.build.build_id,
                expected_current_build_id=expected_current_build_id,
            )
        except LLMWikiCompatibilityError:
            current = self._adapter.current_native_build()
            if current is None or current.build_id != prepared.build.build_id:
                raise
        current = self._adapter.current_native_build()
        if current is None or current.build_id != prepared.build.build_id:
            raise LLMWikiCompatibilityError("LLMWIKI candidate activation did not persist")
        if _native_build_digest(current) != prepared.native_digest:
            raise LLMWikiCompatibilityError("activated LLMWIKI build differs from validation")
        with self._lock:
            latest = self._adapter.current_native_build()
            if latest is None or latest.build_id != prepared.build.build_id:
                raise LLMWikiCompatibilityError("LLMWIKI current changed before catalog promotion")
            self._promote(current, prepared.repository)
        self._record_digest(
            prepared,
            parent_build_id=expected_current_build_id,
            status="active",
        )
        return prepared.repository

    def recover_committed_candidate(self, prepared: PreparedNativeBuild) -> bool:
        self._verify_persisted_digest(prepared)
        current = self._adapter.current_native_build()
        if current is None or current.build_id != prepared.build.build_id:
            return False
        if _native_build_digest(current) != prepared.native_digest:
            raise LLMWikiCompatibilityError(
                "committed LLMWIKI build differs from the validated candidate"
            )
        prepared.repository.load(validate_sources=True)
        with self._lock:
            latest = self._adapter.current_native_build()
            if latest is None or latest.build_id != prepared.build.build_id:
                return False
            if _native_build_digest(latest) != prepared.native_digest:
                raise LLMWikiCompatibilityError("committed LLMWIKI build changed during recovery")
            self._promote(latest, prepared.repository)
        self._record_digest(prepared, parent_build_id=None, status="active")
        return True

    def _load(self, build: NativeBuild, *, validate_sources: bool) -> None:
        expected_digest = self._state.llmwiki_build_digest(build.build_id)
        actual_digest = _native_build_digest(build)
        if expected_digest is not None and actual_digest != expected_digest:
            raise LLMWikiCompatibilityError(
                "current LLMWIKI build differs from its persisted validation"
            )
        prepared = self._prepare(build, validate_sources=validate_sources)
        self._record_digest(prepared, parent_build_id=None, status="active")
        self._promote(build, prepared.repository)

    def _record_digest(
        self,
        prepared: PreparedNativeBuild,
        *,
        parent_build_id: str | None,
        status: str,
    ) -> None:
        try:
            self._state.record_llmwiki_build_digest(
                build_id=prepared.build.build_id,
                native_digest=prepared.native_digest,
                parent_build_id=parent_build_id,
                status=status,
            )
        except Exception as error:
            raise LLMWikiCompatibilityError(
                "LLMWIKI validated build digest could not be persisted"
            ) from error

    def _verify_persisted_digest(self, prepared: PreparedNativeBuild) -> None:
        expected_digest = self._state.llmwiki_build_digest(prepared.build.build_id)
        if expected_digest != prepared.native_digest:
            raise LLMWikiCompatibilityError(
                "LLMWIKI candidate lacks its persisted validation digest"
            )

    def _prepare(
        self,
        build: NativeBuild,
        *,
        validate_sources: bool = True,
    ) -> PreparedNativeBuild:
        native_digest = _native_build_digest(build)
        compatibility_root = self._adapter.materialize(build)
        repository = KnowledgeRepository(compatibility_root, self._source_resolver)
        repository.load(validate_sources=validate_sources)
        if repository.version != build.build_id:
            raise LLMWikiCompatibilityError("compatibility cache version mismatch")
        if _native_build_digest(build) != native_digest:
            raise LLMWikiCompatibilityError("LLMWIKI candidate changed during validation")
        return PreparedNativeBuild(
            build=build,
            compatibility_root=compatibility_root,
            repository=repository,
            native_digest=native_digest,
        )

    def _promote(self, build: NativeBuild, repository: KnowledgeRepository) -> None:
        self._native_build = build
        self._repository = repository


class NativeLLMWikiPipeline:
    """Stage, validate, and atomically activate native LLMWIKI builds."""

    def __init__(
        self,
        *,
        catalog: NativeLLMWikiCatalog,
        file_store: SafeSourceFileStore,
        state_store: StateStore,
        lock_manager: DocumentLockManager,
        doc2md: Doc2MdClient | None = None,
    ) -> None:
        self._catalog = catalog
        self._files = file_store
        self._state = state_store
        self._locks = lock_manager
        self._doc2md = doc2md
        self._lock = asyncio.Lock()

    async def process_pending(self) -> str:
        async with self._lock:
            events = self._state.pending_events()
            if not events:
                return self._catalog.snapshot().version
            event_ids = [event.event_id for event in events]
            self._state.begin_event_batch(event_ids)
            parent_version = self._catalog.snapshot().version
            new_version: str | None = None
            document_ids: set[str] = set()
            prepared: PreparedNativeBuild | None = None
            try:
                self._state.record_sync_task(event_ids, stage="convert", status="running")
                document_ids = await asyncio.to_thread(self._apply_inputs, events)
                self._state.record_sync_task(event_ids, stage="convert", status="succeeded")
                if not document_ids:
                    await asyncio.to_thread(self._verify_live_sources, events)
                    self._state.complete_event_batch(event_ids, parent_version)
                    return parent_version
                self._state.record_sync_task(event_ids, stage="publish", status="running")
                # Gemini enrichment and immutable candidate construction may involve slow
                # network retries. Do them before the short global source-write barrier;
                # prepare_candidate and the live-source check below revalidate the snapshot.
                await asyncio.to_thread(self._verify_live_sources, events)
                new_version = await asyncio.to_thread(self._catalog.stage)
                async with self._locks.acquire_publish():
                    prepared = await asyncio.to_thread(
                        self._catalog.prepare_candidate,
                        new_version,
                        expected_current_build_id=parent_version,
                        changed_document_ids=document_ids,
                    )
                    await asyncio.to_thread(self._verify_live_sources, events)
                    repository = await asyncio.to_thread(
                        self._catalog.activate_candidate,
                        prepared,
                        expected_current_build_id=parent_version,
                    )
                if repository.version != new_version:
                    raise LLMWikiCompatibilityError("published LLMWIKI version was not loaded")
                self._complete_published(
                    event_ids=event_ids,
                    document_ids=document_ids,
                    parent_version=parent_version,
                    new_version=new_version,
                )
                return new_version
            except Exception as error:
                error_code = "KNOWLEDGE_SYNC_FAILED"
                error_message = "LLMWIKI 동기화가 완료되지 않았습니다. 재시도할 수 있습니다."
                error_retryable = True
                if isinstance(error, Doc2MdError):
                    error_code = error.code
                    error_message = str(error)
                    error_retryable = error.retryable
                committed = False
                if prepared is not None:
                    try:
                        async with self._locks.acquire_publish():
                            committed = await asyncio.to_thread(
                                self._catalog.recover_committed_candidate,
                                prepared,
                            )
                    except Exception as integrity_error:
                        raise LLMWikiCompatibilityError(
                            "LLMWIKI publish committed with an invalid candidate"
                        ) from integrity_error
                if committed and new_version is not None:
                    try:
                        self._complete_published(
                            event_ids=event_ids,
                            document_ids=document_ids,
                            parent_version=parent_version,
                            new_version=new_version,
                        )
                    except Exception as completion_error:
                        raise LLMWikiCompatibilityError(
                            "LLMWIKI publish committed but state completion must be retried"
                        ) from completion_error
                    return new_version
                with suppress(Exception):
                    await asyncio.to_thread(
                        self._catalog.refresh,
                        allow_stale_sources=True,
                    )
                self._state.fail_event_batch(
                    event_ids,
                    code=error_code,
                    message=error_message,
                    retryable=error_retryable,
                )
                raise LLMWikiCompatibilityError(str(error)) from error

    def _complete_published(
        self,
        *,
        event_ids: list[str],
        document_ids: set[str],
        parent_version: str,
        new_version: str,
    ) -> None:
        self._state.record_sync_task(
            event_ids,
            stage="publish",
            status="succeeded",
            output={"graph_version": new_version, "document_ids": sorted(document_ids)},
        )
        self._state.record_knowledge_version(
            version=new_version,
            parent_version=parent_version,
            event_ids=event_ids,
            status="active",
        )
        self._state.complete_event_batch(event_ids, new_version)

    def _apply_inputs(self, events: list[PendingEvent]) -> set[str]:
        payloads_by_document: dict[str, list[ContentChangedPayload]] = defaultdict(list)
        documents_to_build: set[str] = set()
        for event in events:
            payload = ContentChangedPayload.model_validate(event.payload)
            if payload.event_id != event.event_id or payload.execution_id != event.execution_id:
                raise LLMWikiCompatibilityError("outbox envelope and payload IDs differ")
            payloads_by_document[payload.document_id].append(payload)

        repository = self._catalog.snapshot()
        for document_id, payloads in payloads_by_document.items():
            manifest = repository.get_manifest(
                document_id,
                access_context=_internal_access_context(),
            )
            if manifest is None:
                raise LLMWikiCompatibilityError(f"unknown LLMWIKI document: {document_id}")
            final_sha256 = payloads[-1].after_sha256
            previous_sha256 = payloads[0].before_sha256
            for payload in payloads:
                if (
                    payload.source_uri != manifest.source.uri
                    or payload.before_sha256 != previous_sha256
                ):
                    raise LLMWikiCompatibilityError(f"event chain mismatch: {document_id}")
                previous_sha256 = payload.after_sha256
            if previous_sha256 != final_sha256:
                raise LLMWikiCompatibilityError(f"event final hash mismatch: {document_id}")
            self._verify_live_source(
                document_id=document_id,
                source_uri=manifest.source.uri,
                expected_sha256=final_sha256,
            )

            active_build_id = repository.version
            baseline = self._catalog.input_baseline(document_id)
            try:
                current_bytes = baseline.path.read_bytes()
            except OSError as error:
                raise LLMWikiCompatibilityError(
                    f"normalized Markdown cannot be read: {document_id}"
                ) from error
            current_sha256 = _sha256(current_bytes)
            if (
                current_sha256 != baseline.file_sha256
                and not self._state.is_known_llmwiki_input_state(
                    document_id=document_id,
                    active_build_id=active_build_id,
                    content_sha256=current_sha256,
                )
            ):
                raise LLMWikiCompatibilityError(
                    f"normalized Markdown contains an unapproved change: {document_id}"
                )

            metadata, body = _parse_normalized_markdown(baseline.text, baseline.path)
            _validate_input_identity(
                metadata,
                document_id=document_id,
                source_uri=manifest.source.uri,
                allowed_source_hashes={manifest.source.sha256},
                revision=manifest.revision,
            )
            if not body.strip():
                raise LLMWikiCompatibilityError(f"normalized Markdown body is empty: {document_id}")

            if manifest.source.sha256 == final_sha256:
                if current_sha256 != baseline.file_sha256 and current_bytes != baseline.text.encode(
                    "utf-8"
                ):
                    self._record_and_write_input(
                        path=baseline.path,
                        text=baseline.text,
                        document_id=document_id,
                        active_build_id=active_build_id,
                        source_sha256=manifest.source.sha256,
                        revision=manifest.revision,
                        event_ids=[payload.event_id for payload in payloads],
                    )
                continue
            if payloads[0].before_sha256 != manifest.source.sha256:
                raise LLMWikiCompatibilityError(f"event base hash mismatch: {document_id}")
            documents_to_build.add(document_id)

            target_revision = manifest.revision
            for _payload in payloads:
                target_revision = _next_revision(target_revision)

            if self._doc2md is not None:
                converted_manifest = manifest.model_copy(
                    update={
                        "revision": target_revision,
                        "source": manifest.source.model_copy(update={"sha256": final_sha256}),
                    }
                )
                converted = self._doc2md.convert(
                    converted_manifest,
                    expected_source_sha256=final_sha256,
                )
                converted_body = _normalize_converted_h1(
                    converted.body,
                    expected_title=manifest.title,
                )
                updated = _render_normalized_markdown(
                    metadata,
                    converted_body,
                    source_sha256=final_sha256,
                    revision=target_revision,
                )
            else:
                updated_body = body
                for payload in payloads:
                    operation = payload.operation
                    expected_count = updated_body.count(operation.expected_text)
                    replacement_count = updated_body.count(operation.replacement_text)
                    if expected_count == operation.expected_occurrences:
                        updated_body = updated_body.replace(
                            operation.expected_text,
                            operation.replacement_text,
                            1,
                        )
                    elif (
                        expected_count == 0 and replacement_count == operation.expected_occurrences
                    ):
                        continue
                    else:
                        raise LLMWikiCompatibilityError(
                            f"normalized Markdown does not match approved operation: {document_id}"
                        )
                updated = _render_normalized_markdown(
                    metadata,
                    updated_body,
                    source_sha256=final_sha256,
                    revision=target_revision,
                )

            updated_metadata, updated_body = _parse_normalized_markdown(updated, baseline.path)
            _validate_input_identity(
                updated_metadata,
                document_id=document_id,
                source_uri=manifest.source.uri,
                allowed_source_hashes={final_sha256},
                revision=target_revision,
            )
            if not updated_body.strip():
                raise LLMWikiCompatibilityError(f"normalized Markdown body is empty: {document_id}")
            self._record_and_write_input(
                path=baseline.path,
                text=updated,
                document_id=document_id,
                active_build_id=active_build_id,
                source_sha256=final_sha256,
                revision=target_revision,
                event_ids=[payload.event_id for payload in payloads],
            )
        return documents_to_build

    def _record_and_write_input(
        self,
        *,
        path: Path,
        text: str,
        document_id: str,
        active_build_id: str,
        source_sha256: str,
        revision: str,
        event_ids: list[str],
    ) -> None:
        self._state.record_llmwiki_input_state(
            document_id=document_id,
            active_build_id=active_build_id,
            source_sha256=source_sha256,
            revision=revision,
            content_sha256=_sha256(text.encode("utf-8")),
            event_ids=event_ids,
        )
        _atomic_write_text(path, text)

    def _verify_live_sources(self, events: list[PendingEvent]) -> None:
        final_by_document: dict[str, ContentChangedPayload] = {}
        for event in events:
            payload = ContentChangedPayload.model_validate(event.payload)
            if payload.event_id != event.event_id or payload.execution_id != event.execution_id:
                raise LLMWikiCompatibilityError("outbox envelope and payload IDs differ")
            final_by_document[payload.document_id] = payload
        for document_id, payload in final_by_document.items():
            self._verify_live_source(
                document_id=document_id,
                source_uri=payload.source_uri,
                expected_sha256=payload.after_sha256,
            )

    def _verify_live_source(
        self,
        *,
        document_id: str,
        source_uri: str,
        expected_sha256: str,
    ) -> None:
        try:
            snapshot = self._files.snapshot(source_uri)
        except SafeFileError as error:
            raise LLMWikiCompatibilityError(
                f"live source cannot be verified: {document_id}: {error.code}"
            ) from error
        if snapshot.sha256 != expected_sha256:
            raise LLMWikiCompatibilityError(f"live source changed before publish: {document_id}")


def _native_ingest(manifest: dict[str, Any], document_id: str) -> NativeIngest:
    ingest = manifest.get("ingest")
    if not isinstance(ingest, dict):
        raise LLMWikiCompatibilityError(f"LLMWIKI ingest metadata is invalid: {document_id}")
    legacy_keys = {"path", "sha256"}
    server_v2_keys = {"aggregate_sha256", "fragments"}
    has_legacy = bool(legacy_keys & set(ingest))
    has_server_v2 = bool(server_v2_keys & set(ingest))
    if has_legacy == has_server_v2:
        raise LLMWikiCompatibilityError(f"LLMWIKI ingest metadata is invalid: {document_id}")

    fragments: tuple[NativeInputFragment, ...]
    mode: str
    aggregate_sha256: str
    if has_legacy:
        if set(ingest) != legacy_keys:
            raise LLMWikiCompatibilityError(f"LLMWIKI ingest metadata is invalid: {document_id}")
        path = _native_input_relative_path(ingest.get("path"), document_id)
        digest = _native_sha256(ingest.get("sha256"), document_id)
        fragments = (NativeInputFragment(chunk_no=None, relative_path=path, sha256=digest),)
        mode = "sections"
        aggregate_sha256 = digest
    else:
        if set(ingest) != server_v2_keys:
            raise LLMWikiCompatibilityError(f"LLMWIKI ingest metadata is invalid: {document_id}")
        aggregate_sha256 = _native_sha256(ingest.get("aggregate_sha256"), document_id)
        raw_fragments = ingest.get("fragments")
        if not isinstance(raw_fragments, list) or not raw_fragments:
            raise LLMWikiCompatibilityError(f"LLMWIKI ingest fragments are invalid: {document_id}")
        parsed: list[NativeInputFragment] = []
        for raw_fragment in raw_fragments:
            if not isinstance(raw_fragment, dict) or set(raw_fragment) != {
                "chunk_no",
                "path",
                "sha256",
            }:
                raise LLMWikiCompatibilityError(
                    f"LLMWIKI ingest fragments are invalid: {document_id}"
                )
            chunk_no = raw_fragment.get("chunk_no")
            if chunk_no is not None and (
                not isinstance(chunk_no, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", chunk_no) is None
            ):
                raise LLMWikiCompatibilityError(
                    f"LLMWIKI ingest fragments are invalid: {document_id}"
                )
            parsed.append(
                NativeInputFragment(
                    chunk_no=chunk_no,
                    relative_path=_native_input_relative_path(
                        raw_fragment.get("path"), document_id
                    ),
                    sha256=_native_sha256(raw_fragment.get("sha256"), document_id),
                )
            )
        fragments = tuple(parsed)
        declared_count = manifest.get("source_fragment_count")
        declared_mode = manifest.get("chunking_mode")
        if (
            not isinstance(declared_count, int)
            or isinstance(declared_count, bool)
            or declared_count != len(fragments)
            or not isinstance(declared_mode, str)
            or declared_mode not in {"sections", "source-fragments"}
        ):
            raise LLMWikiCompatibilityError(f"LLMWIKI ingest metadata is invalid: {document_id}")
        mode = declared_mode
        paths = [fragment.relative_path for fragment in fragments]
        if len(paths) != len(set(paths)):
            raise LLMWikiCompatibilityError(f"LLMWIKI ingest fragments are invalid: {document_id}")
        if mode == "sections":
            if (
                len(fragments) != 1
                or fragments[0].chunk_no is not None
                or aggregate_sha256 != fragments[0].sha256
            ):
                raise LLMWikiCompatibilityError(
                    f"LLMWIKI section ingest metadata is invalid: {document_id}"
                )
        else:
            chunk_numbers = [fragment.chunk_no for fragment in fragments]
            if (
                any(chunk_no is None for chunk_no in chunk_numbers)
                or len(chunk_numbers) != len(set(chunk_numbers))
                or list(chunk_numbers) != sorted(chunk_numbers, key=_natural_key)
                or aggregate_sha256 != _source_fragment_aggregate_sha256(fragments)
            ):
                raise LLMWikiCompatibilityError(
                    f"LLMWIKI source-fragment ingest metadata is invalid: {document_id}"
                )

    descriptor = {
        "chunking_mode": mode,
        "fragments": [
            {
                "chunk_no": fragment.chunk_no,
                "path": fragment.relative_path,
                "sha256": fragment.sha256,
            }
            for fragment in fragments
        ],
    }
    descriptor_sha256 = _sha256(
        b"codegate-llmwiki-ingest-v1\0"
        + json.dumps(
            descriptor,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return NativeIngest(
        chunking_mode=str(mode),
        fragments=fragments,
        descriptor_sha256=descriptor_sha256,
    )


def _native_input_relative_path(value: object, document_id: str) -> str:
    if not isinstance(value, str) or not value.startswith("source-md/"):
        raise LLMWikiCompatibilityError(f"LLMWIKI ingest path is invalid: {document_id}")
    relative = Path(value.removeprefix("source-md/"))
    if (
        relative.is_absolute()
        or len(relative.parts) != 1
        or relative.suffix.lower() != ".md"
        or relative.name in {"", ".", ".."}
    ):
        raise LLMWikiCompatibilityError(f"LLMWIKI ingest path is invalid: {document_id}")
    return f"source-md/{relative.name}"


def _native_sha256(value: object, document_id: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise LLMWikiCompatibilityError(f"LLMWIKI ingest checksum is invalid: {document_id}")
    return value


def _source_fragment_aggregate_sha256(
    fragments: tuple[NativeInputFragment, ...],
) -> str:
    material = [
        {"chunk_no": fragment.chunk_no, "sha256": fragment.sha256} for fragment in fragments
    ]
    return _sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _natural_key(value: object) -> tuple[tuple[int, int | str], ...]:
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part.casefold())
        for part in re.split(r"(\d+)", str(value))
        if part
    )


def _native_build_digest(build: NativeBuild) -> str:
    digest = hashlib.sha256()
    for path in sorted(build.build_dir.rglob("*")):
        if path.is_symlink():
            raise LLMWikiCompatibilityError("LLMWIKI candidate contains a symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise LLMWikiCompatibilityError("LLMWIKI candidate contains a non-regular file")
        relative = path.relative_to(build.build_dir).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _convert_chunks(
    native_chunks: list[dict[str, Any]],
    *,
    manifests: dict[str, dict[str, Any]],
    file_versions: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], list[dict[str, Any]]]]:
    section_ordinals: dict[tuple[str, str], int] = defaultdict(int)
    converted: list[dict[str, Any]] = []
    by_section: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for native in sorted(
        native_chunks, key=lambda item: (str(item["doc_id"]), int(item["ordinal"]))
    ):
        document_id = str(native["doc_id"])
        section_id = str(native["section_id"])
        manifest = manifests.get(document_id)
        if manifest is None:
            raise LLMWikiCompatibilityError("LLMWIKI chunk references an unknown document")
        key = (document_id, section_id)
        ordinal = section_ordinals[key]
        section_ordinals[key] += 1
        suffix = f":{ordinal}" if ordinal else ""
        chunk_id = f"{document_id}@{manifest['revision']}#{section_id}{suffix}"
        record = {
            "schema_version": "1.0.0",
            "chunk_id": chunk_id,
            "document_id": document_id,
            "file_version_id": file_versions[document_id],
            "section_id": section_id,
            "section": native["heading_path"][-1],
            "heading_path": native["heading_path"],
            "text": native["text"],
            "embedding_text": native["embedding_text"],
            "text_sha256": str(native["content_sha256"]).lower(),
            "ordinal": ordinal,
        }
        converted.append(record)
        by_section[key].append(record)
    return converted, by_section


def _convert_links(
    native_links: list[dict[str, Any]],
    chunks_by_section: dict[tuple[str, str], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for native in native_links:
        source_id = str(native["from_doc_id"])
        section_id = str(native["from_section_id"])
        quote = str(native["evidence_quote"])
        matches = [
            chunk["chunk_id"]
            for chunk in chunks_by_section.get((source_id, section_id), [])
            if quote in str(chunk["text"])
        ]
        if len(matches) != 1:
            raise LLMWikiCompatibilityError(
                f"LLMWIKI link evidence is ambiguous: {source_id}#{section_id}"
            )
        converted.append(
            {
                "schema_version": "1.0.0",
                "source_id": source_id,
                "target_id": native["to_doc_id"],
                "relation": str(native["relation_type"]).upper(),
                "status": "VERIFIED" if native["origin"] == "explicit" else "PROPOSED",
                "evidence_chunk_ids": matches,
            }
        )
    return converted


def _convert_aliases(
    native_aliases: dict[str, Any], manifests: list[dict[str, Any]]
) -> dict[str, list[str]]:
    titles = {str(item["id"]): str(item["title"]) for item in manifests}
    converted: dict[str, list[str]] = {}
    for entry in native_aliases.get("entries", []):
        term = str(entry["term"])
        expansions = [titles[str(target["doc_id"])] for target in entry["targets"]]
        converted[term] = list(dict.fromkeys(expansions))
    return converted


def _internal_access_context() -> AccessContext:
    return AccessContext(
        subject_id="codegate-system",
        tenant_id="local",
        readable_access=frozenset(AccessLevel),
        writable_document_ids=frozenset(),
        provisioned=True,
        authz_source="llmwiki-pipeline",
    )


def _rewrite_native_anchors(
    markdown: str,
    section_ids: frozenset[str],
    *,
    document_id: str,
    keep_section_anchors: bool,
) -> str:
    """Keep one evidence anchor per heading while preserving legacy fragment targets."""
    seen_section_ids: set[str] = set()
    output: list[str] = []
    for line in markdown.splitlines(keepends=True):
        line_without_ending = line.rstrip("\r\n")
        match = _STANDALONE_NATIVE_ANCHOR.fullmatch(line_without_ending)
        if match is None:
            output.append(line)
            continue
        anchor = match.group("id")
        if anchor in section_ids:
            if anchor in seen_section_ids:
                raise LLMWikiCompatibilityError(
                    f"LLMWIKI canonical has a duplicate section anchor: {document_id}#{anchor}"
                )
            seen_section_ids.add(anchor)
            if keep_section_anchors:
                output.append(line)
            continue
        if anchor.startswith("sec-"):
            raise LLMWikiCompatibilityError(
                f"LLMWIKI canonical has an unknown section anchor: {document_id}#{anchor}"
            )
        if keep_section_anchors:
            line_ending = line[len(line_without_ending) :]
            output.append(f'<span id="{anchor}"></span>{line_ending}')
        else:
            output.append(line)

    missing = section_ids - seen_section_ids
    if missing:
        raise LLMWikiCompatibilityError(
            f"LLMWIKI canonical lacks section anchors: {document_id}: {', '.join(sorted(missing))}"
        )
    return "".join(output)


def _parse_normalized_markdown(text: str, path: Path) -> tuple[dict[str, Any], str]:
    if len(text.encode("utf-8")) > 10_485_760:
        raise LLMWikiCompatibilityError(f"normalized Markdown is too large: {path.name}")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise LLMWikiCompatibilityError(f"normalized Markdown front matter is missing: {path.name}")
    end = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if end is None:
        raise LLMWikiCompatibilityError(
            f"normalized Markdown front matter is incomplete: {path.name}"
        )
    try:
        metadata = yaml.safe_load("".join(lines[1:end]))
    except yaml.YAMLError as error:
        raise LLMWikiCompatibilityError(
            f"normalized Markdown front matter is invalid: {path.name}"
        ) from error
    if not isinstance(metadata, dict) or not all(isinstance(key, str) for key in metadata):
        raise LLMWikiCompatibilityError(
            f"normalized Markdown front matter must be an object: {path.name}"
        )
    return metadata, "".join(lines[end + 1 :])


def _render_normalized_markdown(
    metadata: dict[str, Any],
    body: str,
    *,
    source_sha256: str,
    revision: str,
) -> str:
    updated = dict(metadata)
    source = updated.get("source")
    if not isinstance(source, dict):
        raise LLMWikiCompatibilityError("normalized Markdown source metadata is invalid")
    updated_source = dict(source)
    updated_source["sha256"] = source_sha256
    updated["source"] = updated_source
    updated["revision"] = revision
    frontmatter = yaml.safe_dump(
        updated,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=1_000,
    )
    return f"---\n{frontmatter}---\n{body}"


def _normalize_converted_h1(body: str, *, expected_title: str) -> str:
    if (
        not expected_title
        or expected_title.strip() != expected_title
        or "\n" in expected_title
        or "\r" in expected_title
    ):
        raise LLMWikiCompatibilityError("LLMWIKI document title is invalid")
    lines = body.splitlines(keepends=True)
    first_content = next(
        (index for index, line in enumerate(lines) if line.strip()),
        None,
    )
    h1_lines: list[int] = []
    fence: tuple[str, int] | None = None
    for index, line in enumerate(lines):
        content = line.rstrip("\r\n")
        fence_match = re.match(r"^\s*(`{3,}|~{3,})", content)
        if fence is not None:
            if (
                fence_match is not None
                and fence_match.group(1)[0] == fence[0]
                and len(fence_match.group(1)) >= fence[1]
            ):
                fence = None
            continue
        if fence_match is not None:
            marker = fence_match.group(1)
            fence = (marker[0], len(marker))
            continue
        if re.fullmatch(r"#\s+\S.*", content) is not None:
            h1_lines.append(index)
    if len(h1_lines) != 1 or first_content != h1_lines[0]:
        raise LLMWikiCompatibilityError("doc2md body must contain exactly one leading H1 heading")
    index = h1_lines[0]
    line = lines[index]
    line_ending = line[len(line.rstrip("\r\n")) :]
    lines[index] = f"# {expected_title}{line_ending}"
    return "".join(lines)


def _validate_input_identity(
    metadata: dict[str, Any],
    *,
    document_id: str,
    source_uri: str,
    allowed_source_hashes: set[str],
    revision: str | None = None,
) -> None:
    source = metadata.get("source")
    if (
        metadata.get("id") != document_id
        or not isinstance(source, dict)
        or source.get("uri") != source_uri
        or source.get("sha256") not in allowed_source_hashes
        or (revision is not None and str(metadata.get("revision")) != revision)
    ):
        raise LLMWikiCompatibilityError(
            f"normalized Markdown identity does not match the active wiki: {document_id}"
        )


def _next_revision(revision: str) -> str:
    try:
        return str(int(revision) + 1)
    except ValueError:
        return f"{revision}+1"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_local_indexes(
    destination: Path,
    version: str,
    chunks: list[dict[str, Any]],
    links: list[dict[str, Any]],
) -> None:
    (destination / "indexes/fts").mkdir(parents=True, exist_ok=True)
    (destination / "indexes/vector").mkdir(parents=True, exist_ok=True)
    (destination / "indexes/graph").mkdir(parents=True, exist_ok=True)
    fts_rows = []
    vector_rows = []
    for chunk in chunks:
        tokens = sorted(set(re.findall(r"[0-9A-Za-z가-힣]+", str(chunk["text"]).lower())))
        fts_rows.append(
            {
                "chunk_id": chunk["chunk_id"],
                "document_id": chunk["document_id"],
                "tokens": tokens,
            }
        )
        vector_rows.append(
            {
                "chunk_id": chunk["chunk_id"],
                "document_id": chunk["document_id"],
                "embedding": _deterministic_embedding(str(chunk["embedding_text"])),
                "adapter": "local-hash-vector-v1",
            }
        )
    _write_jsonl(destination / "indexes/fts/documents.jsonl", fts_rows)
    _write_jsonl(destination / "indexes/vector/embeddings.jsonl", vector_rows)
    _write_jsonl(destination / "indexes/graph/edges.jsonl", links)
    metadata = {
        "schema_version": "1.0.0",
        "version": version,
        "source": "llm-wiki-builder",
        "fts": "local-token-index-v1",
        "vector": "local-hash-vector-v1",
        "graph": "llmwiki-links-v1",
    }
    path = destination / "indexes/index-meta.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json(metadata) + "\n", encoding="utf-8")


def _write_compatibility_schemas(destination: Path) -> None:
    schemas = {
        "agent-guide": AgentGuide.model_json_schema(),
        "chunk": Chunk.model_json_schema(),
        "link": Link.model_json_schema(),
        "manifest": ManifestEntry.model_json_schema(),
        "source-reference": SourceReference.model_json_schema(),
    }
    target = destination / "schemas"
    target.mkdir(parents=True, exist_ok=True)
    for name, schema in schemas.items():
        (target / f"{name}.schema.json").write_text(
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _agent_guide() -> str:
    return """+++
schema_version = "1.0.0"
required_citations = ["document_id", "revision", "graph_version", "chunk_id", "section_id"]
document_content_trust = "untrusted"
read_acl_stage = "before_retrieval"
write_authority = "application_approval_only"
relation_statuses = ["VERIFIED"]
max_evidence_chunks = 5
+++

# Agent guide

The Backend validates only the fixed TOML policy above. Native wiki prose is never injected as
system instructions.
"""


def _builder_root(project_root: Path) -> Path:
    resolved = project_root.resolve()
    if (resolved / "wiki-builder/src/wiki_builder").is_dir():
        return resolved / "wiki-builder"
    if (resolved / "src/wiki_builder").is_dir():
        return resolved
    raise LLMWikiCompatibilityError(f"LLMWIKI project root is invalid: {resolved}")


def _native_validator(build_dir: Path, name: str) -> Draft202012Validator:
    schema_path = _safe_child(build_dir, f"schemas/{name}.schema.json")
    schema = _read_json_object(schema_path)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _raise_schema_errors(
    validator: Draft202012Validator,
    value: Any,
    label: str,
) -> None:
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.absolute_path))
    if errors:
        location = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
        raise LLMWikiCompatibilityError(
            f"LLMWIKI schema validation failed at {label}:{location}: {errors[0].message}"
        )


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LLMWikiCompatibilityError(f"invalid JSON file: {path}") from error
    if not isinstance(value, dict):
        raise LLMWikiCompatibilityError(f"JSON object required: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise LLMWikiCompatibilityError(f"cannot read JSONL: {path}") from error
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise LLMWikiCompatibilityError(f"invalid JSONL: {path}:{line_number}") from error
        if not isinstance(value, dict):
            raise LLMWikiCompatibilityError(f"JSONL object required: {path}:{line_number}")
        records.append(value)
    return records


def _safe_child(root: Path, relative_name: str) -> Path:
    resolved_root, relative, candidate = _safe_path_parts(root, relative_name)
    _reject_symlink_components(resolved_root, relative, relative_name)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise LLMWikiCompatibilityError(
            f"LLMWIKI file is missing or unsafe: {relative_name}"
        ) from error
    if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
        raise LLMWikiCompatibilityError(f"LLMWIKI file is missing or unsafe: {relative_name}")
    return resolved


def _safe_directory_child(root: Path, relative_name: str) -> Path:
    resolved_root, relative, candidate = _safe_path_parts(root, relative_name)
    _reject_symlink_components(resolved_root, relative, relative_name)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise LLMWikiCompatibilityError(
            f"LLMWIKI directory is missing or unsafe: {relative_name}"
        ) from error
    if not resolved.is_relative_to(resolved_root) or not resolved.is_dir():
        raise LLMWikiCompatibilityError(f"LLMWIKI directory is missing or unsafe: {relative_name}")
    return resolved


def _safe_output_child(root: Path, relative_name: str) -> Path:
    resolved_root, relative, candidate = _safe_path_parts(root, relative_name)
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(resolved_root):
        raise LLMWikiCompatibilityError(f"unsafe LLMWIKI output path: {relative_name}")
    _reject_symlink_components(resolved_root, relative, relative_name)
    return candidate


def _safe_path_parts(root: Path, relative_name: str) -> tuple[Path, Path, Path]:
    resolved_root = root.resolve()
    relative = Path(relative_name)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise LLMWikiCompatibilityError(f"unsafe LLMWIKI path: {relative_name}")
    return resolved_root, relative, resolved_root / relative


def _reject_symlink_components(root: Path, relative: Path, label: str) -> None:
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise LLMWikiCompatibilityError(f"LLMWIKI symlink is forbidden: {label}")


def _media_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".md", ".markdown"}:
        return "text/markdown"
    if suffix == ".txt":
        return "text/plain"
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _atomic_write_text(path: Path, content: str) -> None:
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        payload = content.encode("utf-8")
        view = memoryview(payload)
        written = 0
        while written < len(view):
            written += os.write(descriptor, view[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _discard_tree(root: Path) -> None:
    if root.is_symlink():
        root.unlink(missing_ok=True)
        return
    if root.is_dir():
        shutil.rmtree(root, ignore_errors=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    directories = [root]
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise LLMWikiCompatibilityError("compatibility cache must not contain symlinks")
        if path.is_dir():
            directories.append(path)
            continue
        if not path.is_file():
            raise LLMWikiCompatibilityError("compatibility cache contains a non-regular file")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _fsync_directory(directory)
