"""Where a document to convert comes from.

v0.1.0 accepted only an absolute server path, which forces doc2md and its caller
to share a filesystem namespace. The backend's deployment notes record this as a
blocker: a separate Railway service cannot see the backend's volume, so a private
network URL alone does not make the path API work.

v0.2 therefore accepts three source kinds. ``path`` keeps the co-located
deployment working unchanged; ``bytes`` and ``uri`` make a separate service
possible.

Fetched content is written to a temp file and deleted in ``finally`` because the
converters need a real filename. Nothing survives the request.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterator, Literal

from pydantic import BaseModel, Field

from app.errors import Doc2MdError
from app.security import resolve_source_path

logger = logging.getLogger("doc2md.sourcing")

# Schemes we are willing to fetch. http(s) is deliberately absent: an
# unauthenticated service that fetches arbitrary URLs is an SSRF pivot into
# whatever private network it sits on.
FETCHABLE_SCHEMES = ("s3://", "gs://", "azure://", "file://")


class SourceSpec(BaseModel):
    """Polymorphic source. Exactly one kind's fields are used."""

    kind: Literal["path", "bytes", "uri"] = "path"
    path: str | None = Field(None, description="kind=path: absolute server path")
    filename: str | None = Field(
        None, description="kind=bytes|uri: original name, used for format detection"
    )
    content_base64: str | None = Field(None, description="kind=bytes: the file itself")
    uri: str | None = Field(None, description="kind=uri: s3:// gs:// azure:// file://")

    def display_name(self) -> str:
        if self.kind == "path" and self.path:
            return Path(self.path).name
        return self.filename or "document"


@contextlib.contextmanager
def materialize(spec: SourceSpec) -> Iterator[Path]:
    """Yield a real filesystem path for ``spec``, cleaning up anything we made."""
    if spec.kind == "path":
        if not spec.path:
            raise Doc2MdError("UNSUPPORTED_SOURCE", "kind=path에는 path가 필요합니다.")
        yield resolve_source_path(spec.path)
        return

    if not spec.filename:
        raise Doc2MdError(
            "UNSUPPORTED_SOURCE",
            "kind=bytes/uri에는 filename이 필요합니다 (형식 판별에 사용).",
        )

    tmpdir = Path(tempfile.mkdtemp(prefix="doc2md_src_"))
    try:
        # Use only the basename: a caller-supplied "../../x" must not escape.
        target = tmpdir / Path(spec.filename).name
        if spec.kind == "bytes":
            target.write_bytes(_decode_bytes(spec))
        else:
            _fetch_uri(spec, target)
        yield target
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _decode_bytes(spec: SourceSpec) -> bytes:
    if not spec.content_base64:
        raise Doc2MdError(
            "UNSUPPORTED_SOURCE", "kind=bytes에는 content_base64가 필요합니다."
        )
    try:
        return base64.b64decode(spec.content_base64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise Doc2MdError(
            "UNSUPPORTED_SOURCE", "content_base64를 디코딩할 수 없습니다.", str(e)
        ) from e


def _fetch_uri(spec: SourceSpec, target: Path) -> None:
    uri = (spec.uri or "").strip()
    if not uri:
        raise Doc2MdError("UNSUPPORTED_SOURCE", "kind=uri에는 uri가 필요합니다.")
    if not uri.startswith(FETCHABLE_SCHEMES):
        raise Doc2MdError(
            "UNSUPPORTED_SOURCE",
            f"지원하지 않는 URI scheme입니다. 허용: {', '.join(FETCHABLE_SCHEMES)}",
        )

    if uri.startswith("file://"):
        # Still goes through the allowlist — a file:// URI is a path in disguise.
        src = resolve_source_path(uri[len("file://") :].lstrip("/") if os.name != "nt" else uri[len("file://") :])
        shutil.copyfile(src, target)
        return

    _fetch_object_storage(uri, target)


def _fetch_object_storage(uri: str, target: Path) -> None:
    """Download via fsspec, which covers s3/gs/azure with one dependency.

    fsspec is an optional extra: object-storage sourcing is only needed for a
    separate-service deployment, and the co-located default should not have to
    install cloud SDKs.
    """
    try:
        import fsspec
    except ImportError as e:
        raise Doc2MdError(
            "UNSUPPORTED_SOURCE",
            "object storage URI를 사용하려면 `pip install 'doc2md[uri]'`가 필요합니다.",
            str(e),
        ) from e

    try:
        with fsspec.open(uri, "rb") as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)
    except FileNotFoundError as e:
        raise Doc2MdError("SOURCE_NOT_FOUND", f"URI를 찾을 수 없습니다: {uri}") from e
    except Exception as e:
        raise Doc2MdError(
            "SOURCE_UNSAFE", "URI에서 원본을 가져오지 못했습니다.", str(e)
        ) from e
