"""Bearer auth and a source-path allowlist.

``/convert`` opens an absolute path from the server's own filesystem. Exposed on
a public network without either control, that is arbitrary file read — the
integration contract says as much: 무인증 absolute path API를 public network에
노출하지 않는다.

Both controls are opt-in via environment, because the supported deployment today
is co-location with the backend on one loopback interface, where they add
nothing. They exist so that a separate-service deployment is possible at all.

  DOC2MD_API_TOKEN     require ``Authorization: Bearer <token>``
  DOC2MD_ALLOWED_ROOTS  os.pathsep-separated roots; paths must resolve inside one
"""

from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path

from fastapi import Header

from app.errors import Doc2MdError

logger = logging.getLogger("doc2md.security")


def _token() -> str | None:
    return os.getenv("DOC2MD_API_TOKEN") or None


def _roots() -> list[Path]:
    raw = os.getenv("DOC2MD_ALLOWED_ROOTS", "").strip()
    if not raw:
        return []
    out = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if part:
            try:
                out.append(Path(part).resolve(strict=False))
            except OSError:
                logger.warning("ignoring unusable allowed root: %s", part)
    return out


def require_token(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency. A no-op unless DOC2MD_API_TOKEN is set."""
    expected = _token()
    if expected is None:
        return
    scheme, _, presented = (authorization or "").partition(" ")
    # compare_digest to keep the check constant-time
    if scheme.lower() != "bearer" or not hmac.compare_digest(presented, expected):
        raise Doc2MdError("UNAUTHORIZED", "유효한 Bearer 토큰이 필요합니다.")


def resolve_source_path(raw: str) -> Path:
    """Resolve a caller-supplied path, rejecting anything outside the allowlist.

    Resolution happens *before* the check so that ``..`` segments and symlinks
    cannot be used to step outside a permitted root.
    """
    try:
        path = Path(raw).resolve(strict=False)
    except OSError as e:
        raise Doc2MdError("SOURCE_UNSAFE", "원본 경로를 해석할 수 없습니다.", str(e)) from e

    roots = _roots()
    if roots and not any(path == r or path.is_relative_to(r) for r in roots):
        # Deliberately does not echo the allowed roots back to the caller.
        raise Doc2MdError("SOURCE_UNSAFE", "허용되지 않은 경로입니다.")

    if not path.is_file():
        raise Doc2MdError("SOURCE_NOT_FOUND", f"파일을 찾을 수 없습니다: {path.name}")
    return path


def posture() -> dict:
    """What protections are actually active — surfaced by the deep health check
    so a misconfigured production deploy is visible instead of silent."""
    roots = _roots()
    return {
        "auth_required": _token() is not None,
        "allowlist_enforced": bool(roots),
        "allowed_root_count": len(roots),
    }
