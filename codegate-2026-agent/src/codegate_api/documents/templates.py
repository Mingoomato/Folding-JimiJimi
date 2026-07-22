from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from codegate_filesystem import ConfinedFileSystem, FileSystemError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from codegate_api.documents.models import DocumentFormat


class TemplateRegistryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _TemplateEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    format: Literal["docx", "pptx"]
    relative_path: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("template path must be confined and relative")
        return normalized


class _TemplateManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    templates: list[_TemplateEntry] = Field(default_factory=list, max_length=100)


@dataclass(frozen=True, slots=True)
class ApprovedTemplate:
    template_id: str
    format: DocumentFormat
    content: bytes
    sha256: str


class DocumentTemplateRegistry:
    def __init__(self, root: Path | None, *, max_bytes: int) -> None:
        self._root = root.resolve() if root is not None else None
        self._max_bytes = max_bytes

    def get(self, template_id: str, *, format_: DocumentFormat) -> ApprovedTemplate:
        if self._root is None:
            raise TemplateRegistryError(
                "unsupported_template",
                "no approved local template registry is configured",
            )
        manifest = self._load_manifest()
        entries = [item for item in manifest.templates if item.template_id == template_id]
        if len(entries) != 1:
            raise TemplateRegistryError(
                "unsupported_template",
                "template_id is not present exactly once in the approved local registry",
            )
        entry = entries[0]
        if DocumentFormat(entry.format) is not format_:
            raise TemplateRegistryError(
                "unsupported_template",
                "approved template format does not match the requested document format",
            )
        try:
            snapshot = ConfinedFileSystem(
                self._root,
                max_bytes=self._max_bytes,
            ).snapshot(PurePosixPath(entry.relative_path))
        except FileSystemError as error:
            raise TemplateRegistryError(
                "unsafe_template",
                "approved template cannot be read through the confined filesystem",
            ) from error
        if snapshot.sha256 != entry.sha256:
            raise TemplateRegistryError(
                "template_hash_conflict",
                "approved template bytes no longer match the manifest",
            )
        return ApprovedTemplate(
            template_id=entry.template_id,
            format=format_,
            content=snapshot.content,
            sha256=snapshot.sha256,
        )

    def _load_manifest(self) -> _TemplateManifest:
        assert self._root is not None
        try:
            raw = (
                ConfinedFileSystem(
                    self._root,
                    max_bytes=1_048_576,
                )
                .snapshot(PurePosixPath("manifest.json"))
                .content
            )
        except FileSystemError as error:
            raise TemplateRegistryError(
                "template_registry_unavailable",
                "approved template manifest is unavailable",
            ) from error
        if len(raw) > 1_048_576:
            raise TemplateRegistryError(
                "template_registry_invalid",
                "approved template manifest exceeds the size limit",
            )
        try:
            manifest = _TemplateManifest.model_validate_json(raw)
        except (ValidationError, ValueError) as error:
            raise TemplateRegistryError(
                "template_registry_invalid",
                "approved template manifest is invalid",
            ) from error
        identifiers = [item.template_id for item in manifest.templates]
        if len(identifiers) != len(set(identifiers)):
            raise TemplateRegistryError(
                "template_registry_invalid",
                "approved template IDs must be unique",
            )
        return manifest
