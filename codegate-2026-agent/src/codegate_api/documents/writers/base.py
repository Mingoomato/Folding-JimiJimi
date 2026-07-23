from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from codegate_api.documents.models import (
    CreationPayload,
    DocumentFormat,
    DocumentOperation,
    StructuralDiff,
    StructureItem,
)


class WriterError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ProposedDocument:
    content: bytes
    structural_diff: list[StructuralDiff]
    warnings: list[str]
    writer_fingerprint: str
    preview_before: bytes | None = None


class DocumentWriter(Protocol):
    format: DocumentFormat
    fingerprint: str

    def read_structure(self, source: bytes) -> list[StructureItem]: ...

    def mutate(
        self,
        source: bytes,
        operations: list[DocumentOperation],
    ) -> ProposedDocument: ...

    def create(
        self,
        payload: CreationPayload,
        *,
        template: bytes | None = None,
    ) -> ProposedDocument: ...
