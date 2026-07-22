from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from claude_agent_sdk import SessionKey, SessionStoreEntry, project_key_for_directory


class LocalJsonlSessionStore:
    """Persist opaque Claude SDK transcript entries inside the app-owned data root."""

    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()
        self._lock = asyncio.Lock()

    async def append(self, key: SessionKey, entries: list[SessionStoreEntry]) -> None:
        if not entries:
            return
        payload = "".join(
            json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n" for entry in entries
        ).encode("utf-8")
        target = self._path_for(key)
        async with self._lock:
            await asyncio.to_thread(self._append_sync, target, payload)

    async def load(self, key: SessionKey) -> list[SessionStoreEntry] | None:
        target = self._path_for(key)
        async with self._lock:
            return await asyncio.to_thread(self._load_sync, target)

    async def contains(self, *, working_directory: Path, session_id: str) -> bool:
        key: SessionKey = {
            "project_key": project_key_for_directory(working_directory.resolve()),
            "session_id": _canonical_session_id(session_id),
        }
        return await self.load(key) is not None

    def _path_for(self, key: SessionKey) -> Path:
        project_key = str(key["project_key"])
        project_digest = hashlib.sha256(project_key.encode("utf-8")).hexdigest()[:32]
        session_id = _canonical_session_id(str(key["session_id"]))
        subpath = key.get("subpath")
        suffix = ""
        if subpath:
            suffix = ".sub-" + hashlib.sha256(str(subpath).encode("utf-8")).hexdigest()[:24]
        target = self._root / project_digest / f"{session_id}{suffix}.jsonl"
        resolved = target.resolve()
        if not resolved.is_relative_to(self._root):
            raise ValueError("Claude session storage path escaped its root")
        return resolved

    @staticmethod
    def _append_sync(target: Path, payload: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target.parent, 0o700)
        descriptor = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("Claude session transcript append made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(target, 0o600)

    @staticmethod
    def _load_sync(target: Path) -> list[SessionStoreEntry] | None:
        try:
            raw = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        entries: list[SessionStoreEntry] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            parsed: Any = json.loads(line)
            if not isinstance(parsed, dict) or not isinstance(parsed.get("type"), str):
                raise ValueError("Claude session transcript contains an invalid entry")
            entries.append(cast(SessionStoreEntry, parsed))
        return entries or None


def _canonical_session_id(value: str) -> str:
    parsed = UUID(value)
    canonical = str(parsed)
    if value.lower() != canonical:
        raise ValueError("Claude session ID must be a canonical UUID")
    return canonical
