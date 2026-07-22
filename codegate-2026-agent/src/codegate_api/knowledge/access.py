from dataclasses import dataclass

from codegate_api.knowledge.schemas import AccessLevel, Editability, ManifestEntry, WriteAccess


@dataclass(frozen=True, slots=True)
class AccessContext:
    """Authorization result created by a trusted authentication adapter."""

    subject_id: str | None
    tenant_id: str | None
    readable_access: frozenset[AccessLevel]
    writable_document_ids: frozenset[str]
    provisioned: bool = False
    allow_all_writes: bool = False
    email: str | None = None
    authz_source: str = "anonymous"

    @classmethod
    def anonymous(cls) -> "AccessContext":
        return cls(
            subject_id=None,
            tenant_id=None,
            readable_access=frozenset({AccessLevel.PUBLIC}),
            writable_document_ids=frozenset(),
            provisioned=False,
            allow_all_writes=False,
            authz_source="anonymous",
        )

    def can_read(self, document: ManifestEntry) -> bool:
        return document.access in self.readable_access

    def can_write(self, document: ManifestEntry) -> bool:
        return (
            self.subject_id is not None
            and self.provisioned
            and (self.allow_all_writes or document.id in self.writable_document_ids)
            and document.write_access is not WriteAccess.NONE
            and document.editability is Editability.EDITABLE
        )
