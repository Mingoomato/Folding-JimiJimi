import hashlib
import json
import re
import tomllib
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from codegate_api.files.resolver import SourceResolutionError, SourceUriResolver
from codegate_api.knowledge.access import AccessContext
from codegate_api.knowledge.evidence import CanonicalEvidenceError, build_section_index
from codegate_api.knowledge.schemas import AgentGuide, Chunk, Link, ManifestEntry, SourceReference
from codegate_api.models import DocumentResult, Evidence, Relation


class KnowledgePackageError(RuntimeError):
    """Raised when the llm-wiki package violates its integrity contract."""


REQUIRED_CHECKSUM_PATHS = frozenset(
    {
        "AGENT_GUIDE.md",
        "README.md",
        "VERSION",
        "manifest.jsonl",
        "retrieval/aliases.json",
        "retrieval/chunks.jsonl",
        "retrieval/links.jsonl",
        "schemas/agent-guide.schema.json",
        "schemas/chunk.schema.json",
        "schemas/link.schema.json",
        "schemas/manifest.schema.json",
        "schemas/source-reference.schema.json",
    }
)


class KnowledgeRepository:
    def __init__(self, package_root: Path, source_resolver: SourceUriResolver) -> None:
        self._root = package_root.resolve()
        self._source_resolver = source_resolver
        self._version = "unknown"
        self._agent_guide: AgentGuide | None = None
        self._documents: dict[str, ManifestEntry] = {}
        self._chunks: list[Chunk] = []
        self._links: list[Link] = []
        self._aliases: dict[str, list[str]] = {}
        self._checksummed_paths: set[str] = set()
        self._index_artifacts_valid: bool | None = None
        self._loaded = False

    @property
    def version(self) -> str:
        self._ensure_loaded()
        return self._version

    @property
    def agent_guide(self) -> AgentGuide:
        self._ensure_loaded()
        assert self._agent_guide is not None
        return self._agent_guide

    @property
    def package_root(self) -> Path:
        return self._root

    @property
    def graph_available(self) -> bool:
        """The active package's validated VERIFIED-link graph can be traversed."""

        self._ensure_loaded()
        return True

    @property
    def index_artifacts_available(self) -> bool:
        self._ensure_loaded()
        assert self._index_artifacts_valid is not None
        return self._index_artifacts_valid

    def _validate_index_artifacts(self) -> bool:
        required = {
            "indexes/fts/documents.jsonl",
            "indexes/vector/embeddings.jsonl",
            "indexes/graph/edges.jsonl",
            "indexes/index-meta.json",
        }
        if not required.issubset(self._checksummed_paths):
            return False
        try:
            metadata = json.loads(self._read_required_text("indexes/index-meta.json"))
            fts_rows = list(self._read_jsonl("indexes/fts/documents.jsonl"))
            vector_rows = list(self._read_jsonl("indexes/vector/embeddings.jsonl"))
            graph_rows = [
                Link.model_validate(row).model_dump(mode="json")
                for row in self._read_jsonl("indexes/graph/edges.jsonl")
            ]
        except (KnowledgePackageError, ValidationError, json.JSONDecodeError):
            return False
        if not isinstance(metadata, dict) or metadata.get("version") != self._version:
            return False
        chunk_ids = {chunk.chunk_id for chunk in self._chunks}
        fts_ids = [row.get("chunk_id") for row in fts_rows]
        vector_ids = [row.get("chunk_id") for row in vector_rows]
        if (
            len(fts_ids) != len(chunk_ids)
            or set(fts_ids) != chunk_ids
            or len(vector_ids) != len(chunk_ids)
            or set(vector_ids) != chunk_ids
        ):
            return False
        expected_graph = [link.model_dump(mode="json") for link in self._links]
        return graph_rows == expected_graph

    def load(self, *, validate_sources: bool = True) -> None:
        checksummed_paths = self._validate_checksums()
        self._checksummed_paths = checksummed_paths
        missing_checksums = REQUIRED_CHECKSUM_PATHS - checksummed_paths
        if missing_checksums:
            missing = ", ".join(sorted(missing_checksums))
            raise KnowledgePackageError(f"required checksum entries are missing: {missing}")
        self._version = self._read_required_text("VERSION").strip()
        if not self._version:
            raise KnowledgePackageError("VERSION must not be empty")
        self._agent_guide = self._read_agent_guide()

        documents = [
            ManifestEntry.model_validate(item) for item in self._read_jsonl("manifest.jsonl")
        ]
        self._documents = {document.id: document for document in documents}
        if len(self._documents) != len(documents):
            raise KnowledgePackageError("manifest contains duplicate document IDs")

        self._chunks = [
            Chunk.model_validate(item) for item in self._read_jsonl("retrieval/chunks.jsonl")
        ]
        self._links = [
            Link.model_validate(item) for item in self._read_jsonl("retrieval/links.jsonl")
        ]
        self._aliases = self._read_aliases()
        self._validate_references(checksummed_paths, validate_sources=validate_sources)
        self._loaded = True
        self._index_artifacts_valid = self._validate_index_artifacts()

    def search(
        self,
        query: str,
        *,
        access_context: AccessContext,
        top_k: int = 5,
    ) -> list[DocumentResult]:
        self._ensure_loaded()
        tokens = self._expanded_tokens(query)
        if not tokens:
            return []

        best_chunks: dict[str, tuple[float, Chunk]] = {}
        source_freshness: dict[str, bool] = {}
        for chunk in self._chunks:
            document = self._documents[chunk.document_id]
            if not self._is_retrievable(document, access_context):
                continue
            if document.id not in source_freshness:
                source_freshness[document.id] = self._source_is_current(document)
            if not source_freshness[document.id]:
                continue
            metadata = " ".join([document.title, *document.tags, *document.aliases]).lower()
            content = " ".join(
                [chunk.text, chunk.embedding_text, chunk.section, *chunk.heading_path]
            ).lower()
            weighted_matches = sum(
                2 if token in metadata else 1 if token in content else 0 for token in tokens
            )
            if weighted_matches == 0:
                continue
            score = weighted_matches / (len(tokens) * 2)
            previous = best_chunks.get(chunk.document_id)
            if previous is None or score > previous[0]:
                best_chunks[chunk.document_id] = (score, chunk)

        doc_type_priority = {"regulation": 0, "manual": 1, "report": 2}
        ranked = sorted(
            best_chunks.items(),
            key=lambda item: (
                -item[1][0],
                doc_type_priority.get(self._documents[item[0]].doc_type, 99),
                item[0],
            ),
        )[:top_k]
        return [
            self._to_result(document_id, score, chunk, access_context)
            for document_id, (score, chunk) in ranked
        ]

    def get(
        self,
        document_id: str,
        *,
        access_context: AccessContext,
    ) -> DocumentResult | None:
        self._ensure_loaded()
        document = self._documents.get(document_id)
        if (
            document is None
            or not self._is_retrievable(document, access_context)
            or not self._source_is_current(document)
        ):
            return None
        chunk = next(chunk for chunk in self._chunks if chunk.document_id == document_id)
        return self._to_result(document_id, 1.0, chunk, access_context)

    def get_manifest(
        self,
        document_id: str,
        *,
        access_context: AccessContext,
    ) -> ManifestEntry | None:
        """Return a defensive copy of an ACL-visible manifest entry."""

        self._ensure_loaded()
        document = self._documents.get(document_id)
        if document is None or not self._is_retrievable(document, access_context):
            return None
        return document.model_copy(deep=True)

    def supports_exact_change(self, document_id: str, expected_text: str) -> bool:
        """Return whether the current local adapter can preserve this edit in evidence."""

        self._ensure_loaded()
        document = self._documents.get(document_id)
        if document is None:
            return False
        canonical = self._safe_package_path(document.canonical_path).read_text(encoding="utf-8")
        if canonical.count(expected_text) != 1:
            return False
        return any(
            expected_text in chunk.text
            or any(expected_text in heading for heading in chunk.heading_path)
            for chunk in self._chunks
            if chunk.document_id == document_id
        )

    def read_canonical_text(
        self,
        document_id: str,
        *,
        access_context: AccessContext,
        max_bytes: int = 262_144,
    ) -> tuple[ManifestEntry, str] | None:
        """Read one validated llm-wiki document without exposing a filesystem path."""

        self._ensure_loaded()
        document = self._documents.get(document_id)
        if (
            document is None
            or not self._is_retrievable(document, access_context)
            or not self._source_is_current(document)
        ):
            return None
        content = self._safe_package_path(document.canonical_path).read_bytes()
        if len(content) > max_bytes:
            raise KnowledgePackageError("canonical document exceeds the agent read limit")
        try:
            return document.model_copy(deep=True), content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise KnowledgePackageError("canonical document is not valid UTF-8") from error

    def read_source_text(
        self,
        document_id: str,
        *,
        access_context: AccessContext,
        max_bytes: int = 262_144,
    ) -> tuple[ManifestEntry, str] | None:
        """Read a current allowlisted UTF-8 source file through its stable document ID."""

        self._ensure_loaded()
        document = self._documents.get(document_id)
        if (
            document is None
            or not self._is_retrievable(document, access_context)
            or not self._source_is_current(document)
            or document.source.media_type not in {"text/markdown", "text/plain"}
        ):
            return None
        source = self._source_resolver.resolve(document.source.uri)
        content = source.read_bytes()
        if len(content) > max_bytes:
            raise KnowledgePackageError("source document exceeds the agent read limit")
        try:
            return document.model_copy(deep=True), content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise KnowledgePackageError("source document is not valid UTF-8") from error

    def _to_result(
        self,
        document_id: str,
        score: float,
        chunk: Chunk,
        access_context: AccessContext,
    ) -> DocumentResult:
        document = self._documents[document_id]
        relations = [
            Relation(relation=link.relation, target_document_id=link.target_id)
            for link in self._links
            if (
                link.source_id == document_id
                and link.status == "VERIFIED"
                and self._is_retrievable(self._documents[link.target_id], access_context)
                and self._source_is_current(self._documents[link.target_id])
            )
        ]
        evidence = Evidence(
            chunk_id=chunk.chunk_id,
            section_id=chunk.section_id,
            section=chunk.section,
            heading_path=chunk.heading_path,
            quote=chunk.text,
        )
        return DocumentResult(
            document_id=document.id,
            file_version_id=document.file_version_id,
            revision=document.revision,
            authority_level=document.authority_level,
            title=document.title,
            display_path=document.source.filename,
            source_uri=document.source.uri,
            score=round(score, 4),
            graph_version=self._version,
            evidence=[evidence],
            citations=[f"[{document.id} rev.{document.revision} §{evidence.section_id}]"],
            relations=relations,
            can_read=True,
            can_write=access_context.can_write(document),
            editability=document.editability.value,
        )

    def _validate_checksums(self) -> set[str]:
        checksum_lines = self._read_required_text("checksums.sha256").splitlines()
        if not checksum_lines:
            raise KnowledgePackageError("checksums.sha256 must not be empty")

        checksummed_paths: set[str] = set()
        for line in checksum_lines:
            try:
                expected, relative_name = line.split(maxsplit=1)
            except ValueError as error:
                raise KnowledgePackageError("invalid checksum line") from error
            relative_name = relative_name.removeprefix("*").strip()
            if relative_name in checksummed_paths:
                raise KnowledgePackageError(f"duplicate checksum entry: {relative_name}")
            path = self._safe_package_path(relative_name)
            if not path.is_file():
                raise KnowledgePackageError(f"checksum target is missing: {relative_name}")
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                raise KnowledgePackageError(f"checksum mismatch: {relative_name}")
            checksummed_paths.add(relative_name)
        return checksummed_paths

    def _validate_references(
        self,
        checksummed_paths: set[str],
        *,
        validate_sources: bool,
    ) -> None:
        chunk_ids = {chunk.chunk_id for chunk in self._chunks}
        if len(chunk_ids) != len(self._chunks):
            raise KnowledgePackageError("chunks contain duplicate chunk IDs")
        section_index_by_document = {}
        for document in self._documents.values():
            if document.canonical_path not in checksummed_paths:
                raise KnowledgePackageError(
                    f"canonical document is not checksummed: {document.canonical_path}"
                )
            canonical = self._safe_package_path(document.canonical_path)
            if not canonical.is_file():
                raise KnowledgePackageError(
                    f"canonical document is missing: {document.canonical_path}"
                )
            try:
                section_index_by_document[document.id] = build_section_index(
                    canonical.read_text(encoding="utf-8")
                )
            except CanonicalEvidenceError as error:
                raise KnowledgePackageError(
                    f"canonical section index is invalid: {document.canonical_path}: {error}"
                ) from error
            source = self._source_resolver.resolve(document.source.uri)
            if not source.is_file():
                raise KnowledgePackageError(f"source document is missing: {document.source.uri}")
            if validate_sources:
                source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
                if source_sha256 != document.source.sha256:
                    raise KnowledgePackageError(f"source checksum mismatch: {document.source.uri}")
            reference_name = f"references/{document.id}.source.json"
            if reference_name not in checksummed_paths:
                raise KnowledgePackageError(
                    f"source reference is not checksummed: {reference_name}"
                )
            try:
                reference = SourceReference.model_validate_json(
                    self._read_required_text(reference_name)
                )
            except ValidationError as error:
                raise KnowledgePackageError(
                    f"invalid source reference: {reference_name}"
                ) from error
            if (
                reference.document_id != document.id
                or reference.source_uri != document.source.uri
                or reference.source_sha256 != document.source.sha256
            ):
                raise KnowledgePackageError(f"source reference mismatch: {reference_name}")

        for chunk in self._chunks:
            if chunk.document_id not in self._documents:
                raise KnowledgePackageError(
                    f"chunk references unknown document: {chunk.document_id}"
                )
            text_sha256 = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
            if text_sha256 != chunk.text_sha256:
                raise KnowledgePackageError(f"chunk checksum mismatch: {chunk.chunk_id}")
            document = self._documents[chunk.document_id]
            ordinal_suffix = f":{chunk.ordinal}" if chunk.ordinal else ""
            expected_chunk_id = (
                f"{document.id}@{document.revision}#{chunk.section_id}{ordinal_suffix}"
            )
            if chunk.chunk_id != expected_chunk_id:
                raise KnowledgePackageError(
                    f"chunk ID does not match document revision: {chunk.chunk_id}"
                )
            if chunk.file_version_id != document.file_version_id:
                raise KnowledgePackageError(f"chunk file version mismatch: {chunk.chunk_id}")
            if chunk.heading_path[-1] != chunk.section:
                raise KnowledgePackageError(f"chunk heading path mismatch: {chunk.chunk_id}")
            if chunk.text not in chunk.embedding_text:
                raise KnowledgePackageError(f"embedding text omits evidence text: {chunk.chunk_id}")
            section = section_index_by_document[chunk.document_id].get(chunk.section_id)
            if section is None:
                raise KnowledgePackageError(
                    f"evidence section is absent from canonical document: {chunk.chunk_id}"
                )
            if tuple(chunk.heading_path) != section.heading_path:
                raise KnowledgePackageError(f"evidence heading path mismatch: {chunk.chunk_id}")
            if section.body.count(chunk.text) != 1:
                raise KnowledgePackageError(
                    f"evidence text is not unique in its canonical section: {chunk.chunk_id}"
                )
        documents_with_chunks = {chunk.document_id for chunk in self._chunks}
        missing_chunks = set(self._documents) - documents_with_chunks
        if missing_chunks:
            raise KnowledgePackageError(
                f"manifest documents lack evidence chunks: {', '.join(sorted(missing_chunks))}"
            )
        for link in self._links:
            if link.source_id not in self._documents or link.target_id not in self._documents:
                raise KnowledgePackageError("link references unknown document")
            if not set(link.evidence_chunk_ids).issubset(chunk_ids):
                raise KnowledgePackageError("link references unknown evidence chunk")

    def _read_agent_guide(self) -> AgentGuide:
        raw_guide = self._read_required_text("AGENT_GUIDE.md")
        if len(raw_guide.encode("utf-8")) > 32_768:
            raise KnowledgePackageError("AGENT_GUIDE.md exceeds 32 KiB")
        sections = raw_guide.split("+++", maxsplit=2)
        if len(sections) != 3 or sections[0].strip() or not sections[2].strip():
            raise KnowledgePackageError("AGENT_GUIDE.md requires TOML frontmatter and a body")
        try:
            metadata = tomllib.loads(sections[1])
            return AgentGuide.model_validate(metadata)
        except (tomllib.TOMLDecodeError, ValidationError) as error:
            raise KnowledgePackageError("invalid AGENT_GUIDE.md frontmatter") from error

    def _read_aliases(self) -> dict[str, list[str]]:
        try:
            value = json.loads(self._read_required_text("retrieval/aliases.json"))
        except json.JSONDecodeError as error:
            raise KnowledgePackageError("invalid retrieval/aliases.json") from error
        if not isinstance(value, dict):
            raise KnowledgePackageError("retrieval/aliases.json must be an object")
        aliases: dict[str, list[str]] = {}
        for alias, expansions in value.items():
            if (
                not isinstance(alias, str)
                or not alias.strip()
                or not isinstance(expansions, list)
                or not expansions
                or not all(isinstance(item, str) and item.strip() for item in expansions)
            ):
                raise KnowledgePackageError("retrieval/aliases.json contains an invalid alias")
            aliases[alias] = expansions
        return aliases

    def _read_jsonl(self, relative_name: str) -> Iterable[dict[str, Any]]:
        path = self._safe_package_path(relative_name)
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise KnowledgePackageError(
                    f"invalid JSONL at {relative_name}:{line_number}"
                ) from error
            if not isinstance(value, dict):
                raise KnowledgePackageError(
                    f"JSONL object required at {relative_name}:{line_number}"
                )
            yield value

    def _read_required_text(self, relative_name: str) -> str:
        path = self._safe_package_path(relative_name)
        if not path.is_file():
            raise KnowledgePackageError(f"required package file is missing: {relative_name}")
        return path.read_text(encoding="utf-8")

    def _safe_package_path(self, relative_name: str) -> Path:
        candidate = (self._root / relative_name).resolve()
        if not candidate.is_relative_to(self._root):
            raise KnowledgePackageError(f"package path escapes root: {relative_name}")
        return candidate

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    @staticmethod
    def _tokens(query: str) -> list[str]:
        return [
            token.lower() for token in re.findall(r"[0-9A-Za-z가-힣]+", query) if len(token) > 1
        ]

    def _expanded_tokens(self, query: str) -> list[str]:
        expanded = [query]
        lowered_query = query.lower()
        for alias, values in self._aliases.items():
            if alias.lower() in lowered_query:
                expanded.extend(values)
        return list(dict.fromkeys(self._tokens(" ".join(expanded))))

    def _source_is_current(self, document: ManifestEntry) -> bool:
        try:
            relative = self._source_resolver.relative_path(document.source.uri)
            candidate = self._source_resolver.source_root.joinpath(*relative.parts)
            current = self._source_resolver.source_root
            for part in relative.parts:
                current = current / part
                if current.is_symlink():
                    return False
            if not candidate.is_file():
                return False
            return hashlib.sha256(candidate.read_bytes()).hexdigest() == document.source.sha256
        except (OSError, SourceResolutionError):
            return False

    @staticmethod
    def _is_retrievable(document: ManifestEntry, access_context: AccessContext) -> bool:
        today = date.today()
        return (
            document.status == "active"
            and (document.effective_from is None or document.effective_from <= today)
            and (document.effective_to is None or today <= document.effective_to)
            and access_context.can_read(document)
        )
