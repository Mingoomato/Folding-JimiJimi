from __future__ import annotations

import io
import zipfile
from collections.abc import Iterable
from pathlib import PurePosixPath

from defusedxml import ElementTree

from codegate_api.documents.models import DocumentFormat


class DocumentPolicyError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DocumentPackageGuard:
    def __init__(
        self,
        *,
        max_source_bytes: int,
        max_result_bytes: int,
        max_parts: int,
        max_expanded_bytes: int,
        max_xml_depth: int = 128,
    ) -> None:
        self._max_source_bytes = max_source_bytes
        self._max_result_bytes = max_result_bytes
        self._max_parts = max_parts
        self._max_expanded_bytes = max_expanded_bytes
        self._max_xml_depth = max_xml_depth

    def inspect_source(self, source: bytes, format_: DocumentFormat) -> None:
        if len(source) > self._max_source_bytes:
            raise DocumentPolicyError("source_too_large", "source exceeds configured size limit")
        if format_ in {
            DocumentFormat.HWPX,
            DocumentFormat.DOCX,
            DocumentFormat.PPTX,
            DocumentFormat.XLSX,
        }:
            self._inspect_zip(source, format_)
        elif format_ is DocumentFormat.PDF:
            self._inspect_pdf_header(source)

    def inspect_result(self, result: bytes, format_: DocumentFormat) -> None:
        if len(result) > self._max_result_bytes:
            raise DocumentPolicyError("result_too_large", "result exceeds configured size limit")
        if format_ in {
            DocumentFormat.HWPX,
            DocumentFormat.DOCX,
            DocumentFormat.PPTX,
            DocumentFormat.XLSX,
        }:
            self._inspect_zip(result, format_, policy_checks=False)
        elif format_ is DocumentFormat.PDF:
            self._inspect_pdf_header(result)

    def _inspect_zip(
        self,
        source: bytes,
        format_: DocumentFormat,
        *,
        policy_checks: bool = True,
    ) -> None:
        try:
            archive = zipfile.ZipFile(io.BytesIO(source))
        except (zipfile.BadZipFile, OSError) as error:
            raise DocumentPolicyError("malformed_package", "invalid ZIP/OPC package") from error
        with archive:
            infos = archive.infolist()
            if len(infos) > self._max_parts:
                raise DocumentPolicyError("package_part_limit", "package contains too many parts")
            expanded = sum(info.file_size for info in infos)
            if expanded > self._max_expanded_bytes:
                raise DocumentPolicyError(
                    "package_expanded_limit",
                    "package expanded size exceeds configured limit",
                )
            names = {info.filename.replace("\\", "/") for info in infos}
            if len(names) != len(infos):
                raise DocumentPolicyError(
                    "duplicate_package_part", "package contains duplicate part names"
                )
            for info in infos:
                name = info.filename.replace("\\", "/")
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or not name or "\x00" in name:
                    raise DocumentPolicyError("unsafe_package_path", "unsafe path in package")
                if info.flag_bits & 0x1:
                    raise DocumentPolicyError(
                        "encrypted_document", "encrypted package is read-only"
                    )
                if name.lower().endswith((".xml", ".rels")):
                    self._validate_xml_depth(archive.read(info))
            if policy_checks:
                self._enforce_format_policy(archive, names, format_)

    def _validate_xml_depth(self, value: bytes) -> None:
        depth = 0
        try:
            for event, _element in ElementTree.iterparse(
                io.BytesIO(value), events=("start", "end")
            ):
                depth += 1 if event == "start" else -1
                if depth > self._max_xml_depth:
                    raise DocumentPolicyError("xml_depth_limit", "XML nesting is too deep")
        except DocumentPolicyError:
            raise
        except ElementTree.ParseError as error:
            raise DocumentPolicyError("malformed_xml", "malformed XML package part") from error

    def _enforce_format_policy(
        self,
        archive: zipfile.ZipFile,
        names: set[str],
        format_: DocumentFormat,
    ) -> None:
        lowered = {name.lower() for name in names}
        if any(
            marker in name
            for name in lowered
            for marker in ("vbaproject", "activex", "embeddings/", "_xmlsignatures")
        ):
            raise DocumentPolicyError(
                "unsupported_active_content",
                "macros, ActiveX, OLE, and digital signatures are read-only",
            )
        if format_ is DocumentFormat.XLSX and any(
            marker in name for name in lowered for marker in ("xl/drawings/", "externallinks/")
        ):
            raise DocumentPolicyError(
                "unsupported_workbook_feature",
                "drawings and external links are read-only",
            )
        if format_ is DocumentFormat.DOCX:
            self._reject_external_relationships(
                archive,
                names,
                relationship_suffixes=("attachedtemplate",),
            )
            self._reject_unknown_docx_parts(names)
        elif format_ is DocumentFormat.PPTX or format_ is DocumentFormat.XLSX:
            self._reject_external_relationships(archive, names)

    def _reject_external_relationships(
        self,
        archive: zipfile.ZipFile,
        names: Iterable[str],
        relationship_suffixes: tuple[str, ...] = (),
    ) -> None:
        for name in names:
            if not name.lower().endswith(".rels"):
                continue
            root = ElementTree.fromstring(archive.read(name))
            for relationship in root:
                target_mode = relationship.attrib.get("TargetMode", "").lower()
                relationship_type = relationship.attrib.get("Type", "").lower()
                if target_mode == "external" or relationship_type.endswith(relationship_suffixes):
                    raise DocumentPolicyError(
                        "external_relationship",
                        "external document relationships are read-only",
                    )

    @staticmethod
    def _reject_unknown_docx_parts(names: set[str]) -> None:
        allowed_prefixes = (
            "word/",
            "docprops/",
            "_rels/",
            "customxml/",
        )
        allowed_exact = {"[Content_Types].xml"}
        unknown = sorted(
            name
            for name in names
            if name not in allowed_exact and not name.lower().startswith(allowed_prefixes)
        )
        if unknown:
            raise DocumentPolicyError(
                "unknown_opc_part",
                f"unsupported DOCX package part: {unknown[0]}",
            )

    @staticmethod
    def _inspect_pdf_header(source: bytes) -> None:
        if not source.startswith(b"%PDF-"):
            raise DocumentPolicyError("malformed_pdf", "invalid PDF header")
