from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from llm_wiki_local.errors import ConversionError

SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
BUILDER_FRONT_MATTER_FIELDS = frozenset(
    {
        "schema_version",
        "id",
        "title",
        "doc_type",
        "language",
        "revision",
        "status",
        "chunk_no",
        "official_number",
        "authority_level",
        "issuing_org",
        "issued_on",
        "effective_from",
        "effective_to",
        "source",
        "access",
        "tags",
        "aliases",
    }
)


@dataclass(frozen=True)
class PreparedDocument:
    metadata: dict[str, Any]
    fragment_files: list[str]


class InputSnapshots:
    def __init__(self, root: Path, bootstrap_input_dir: Path):
        self.root = root
        self.bootstrap_input_dir = bootstrap_input_dir
        self.root.mkdir(parents=True, exist_ok=True)

    def create_candidate(self, job_id: str, active_input_dir: Path | None) -> Path:
        safe_job_id = re.sub(r"[^A-Za-z0-9._-]", "_", job_id)
        candidate = self.root / f".candidate-{safe_job_id}"
        if candidate.exists():
            shutil.rmtree(candidate)
        source = active_input_dir or self.bootstrap_input_dir
        candidate.mkdir(parents=True)
        if source.exists():
            if not source.is_dir():
                raise ConversionError(f"Markdown input source is not a directory: {source}")
            for path in sorted(source.glob("*.md")):
                if path.is_symlink() or not path.is_file():
                    continue
                shutil.copy2(path, candidate / path.name)
        elif active_input_dir is not None:
            raise ConversionError(f"active Markdown input snapshot is missing: {source}")
        return candidate

    def replace_document(
        self,
        candidate: Path,
        *,
        old_fragment_files: list[str],
        result: dict[str, Any],
        doc_id: str,
        relative_path: str,
        source_sha256: str,
        requested_metadata: dict[str, Any],
    ) -> PreparedDocument:
        self.remove_fragments(
            candidate,
            old_fragment_files,
            require_all=bool(old_fragment_files),
        )
        pieces = self._pieces(result)
        written: list[str] = []
        normalized_metadata: dict[str, Any] | None = None
        for ordinal, piece in enumerate(pieces, start=1):
            body = piece.get("body")
            frontmatter = piece.get("frontmatter")
            if not isinstance(body, str) or not body.strip() or not isinstance(frontmatter, dict):
                raise ConversionError("doc2md returned an invalid Markdown chunk")
            chunk_no = f"{doc_id}_{ordinal:03d}"
            if not SAFE_NAME_RE.fullmatch(chunk_no):
                raise ConversionError(f"unsafe generated chunk_no: {chunk_no}")
            metadata = self._normalize_frontmatter(
                frontmatter,
                requested_metadata=requested_metadata,
                doc_id=doc_id,
                chunk_no=chunk_no,
                relative_path=relative_path,
                source_sha256=source_sha256,
            )
            if normalized_metadata is None:
                normalized_metadata = dict(metadata)
                normalized_metadata.pop("chunk_no", None)
            filename = f"{chunk_no}.md"
            destination = candidate / filename
            if ordinal == 1:
                body = self._align_first_h1(body, str(metadata["title"]))
            rendered = self._render(metadata, body)
            self._atomic_write(destination, rendered)
            written.append(filename)
        if normalized_metadata is None:
            raise ConversionError("doc2md returned no Markdown chunks")
        return PreparedDocument(metadata=normalized_metadata, fragment_files=written)

    @staticmethod
    def remove_fragments(
        candidate: Path,
        fragment_files: list[str],
        *,
        require_all: bool = False,
    ) -> None:
        missing: list[str] = []
        for filename in fragment_files:
            if not SAFE_NAME_RE.fullmatch(filename) or Path(filename).name != filename:
                raise ConversionError(f"unsafe recorded fragment filename: {filename}")
            path = candidate / filename
            if path.exists():
                path.unlink()
            else:
                missing.append(filename)
        if require_all and missing:
            raise ConversionError(
                "recorded Markdown fragments are missing: " + ", ".join(sorted(missing))
            )

    def promote(self, candidate: Path, build_id: str) -> Path:
        safe_build_id = re.sub(r"[^A-Za-z0-9._-]", "_", build_id)
        target = self.root / f"snapshot-{safe_build_id}"
        if target.exists():
            shutil.rmtree(candidate)
            return target
        os.replace(candidate, target)
        return target

    @staticmethod
    def discard(candidate: Path | None) -> None:
        if candidate is not None and candidate.exists():
            shutil.rmtree(candidate)

    @staticmethod
    def _pieces(result: dict[str, Any]) -> list[dict[str, Any]]:
        chunks = result.get("chunks")
        if isinstance(chunks, list) and chunks:
            return [item for item in chunks if isinstance(item, dict)]
        frontmatter = result.get("frontmatter")
        body = result.get("body")
        if isinstance(frontmatter, dict) and isinstance(body, str):
            return [{"frontmatter": frontmatter, "body": body}]
        return []

    @staticmethod
    def _normalize_frontmatter(
        source: dict[str, Any],
        *,
        requested_metadata: dict[str, Any],
        doc_id: str,
        chunk_no: str,
        relative_path: str,
        source_sha256: str,
    ) -> dict[str, Any]:
        # doc2md also emits converter provenance such as canonical_sha256 and
        # converter_version. The wiki-builder contract rejects unknown fields, so
        # only copy fields from its explicit front-matter allowlist.
        metadata = {
            key: value for key, value in source.items() if key in BUILDER_FRONT_MATTER_FIELDS
        }
        metadata.update(
            {
                key: value
                for key, value in requested_metadata.items()
                if value is not None and key in BUILDER_FRONT_MATTER_FIELDS and key != "id"
            }
        )
        metadata["schema_version"] = "1.0.0"
        metadata["id"] = doc_id
        metadata["chunk_no"] = chunk_no
        if metadata.get("doc_type") == "document":
            metadata["doc_type"] = "general"
        metadata.setdefault("doc_type", "general")
        metadata.setdefault("revision", "1")
        metadata["revision"] = str(metadata["revision"])
        metadata.setdefault("status", "active")
        metadata.setdefault("official_number", None)
        metadata.setdefault("authority_level", None)
        metadata.setdefault("issuing_org", None)
        metadata.setdefault("issued_on", None)
        metadata.setdefault("effective_from", None)
        metadata.setdefault("effective_to", None)
        metadata.setdefault("access", "internal")
        metadata.setdefault("tags", [])
        metadata.setdefault("aliases", [])
        metadata["source"] = {
            "filename": Path(relative_path).name,
            "uri": requested_metadata.get("uri") or f"source://{relative_path}",
            "sha256": source_sha256,
            "verification": "verified",
        }
        return metadata

    @staticmethod
    def _render(metadata: dict[str, Any], body: str) -> str:
        yaml_block = yaml.safe_dump(
            metadata,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        normalized_body = body.replace("\r\n", "\n").replace("\r", "\n").strip()
        return f"---\n{yaml_block}---\n\n{normalized_body}\n"

    @staticmethod
    def _align_first_h1(body: str, title: str) -> str:
        lines = body.replace("\r\n", "\n").replace("\r", "\n").splitlines()
        first_content = next((index for index, line in enumerate(lines) if line.strip()), None)
        if first_content is None:
            return body
        if re.fullmatch(r"#\s+.+", lines[first_content].strip()):
            lines[first_content] = f"# {title}"
            return "\n".join(lines)
        return body

    @staticmethod
    def _atomic_write(path: Path, value: str) -> None:
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(value, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
