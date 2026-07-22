"""Writable doc2md state paths.

Runtime state must not live beside the installed Python package: packaged apps and
site-packages are commonly read-only. ``DOC2MD_DATA_ROOT`` lets the desktop
supervisor place all state inside its private runtime directory.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def data_root() -> Path:
    configured = os.getenv("DOC2MD_DATA_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/CODEGATE/doc2md"
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        return base / "CODEGATE/doc2md"
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return base / "codegate/doc2md"


def state_path(*parts: str) -> Path:
    return data_root().joinpath(*parts)


def atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace a UTF-8 state file in its destination directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
