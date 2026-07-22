from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import ParamSpec, TypeVar

from codegate_filesystem import (
    ConfinedFileSystem,
    FileSystemConflict,
    FileSystemError,
    file_lock,
)
from codegate_filesystem import (
    FileSnapshot as AdapterSnapshot,
)

from codegate_api.files.resolver import SourceUriResolver


class SafeFileError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FileHashConflict(SafeFileError):
    def __init__(self) -> None:
        super().__init__("SOURCE_HASH_CONFLICT", "원본 파일이 변경안 생성 이후 바뀌었습니다.")


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    source_uri: str
    relative_path: PurePosixPath
    content: bytes
    sha256: str
    mode: int


FaultHook = Callable[[str], None]
P = ParamSpec("P")
R = TypeVar("R")


class DocumentLockManager:
    """Combine async process-local coordination with cross-process file locks."""

    def __init__(self, lock_root: Path) -> None:
        self._lock_root = lock_root.resolve()
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()
        self._publish_barrier = _AsyncReaderWriterLock()

    @asynccontextmanager
    async def acquire_publish(self) -> AsyncIterator[None]:
        async with (
            self._publish_barrier.write(),
            self._file_lock(".wiki-publish.lock", shared=False),
        ):
            yield

    @asynccontextmanager
    async def acquire(self, document_id: str) -> AsyncIterator[None]:
        if not document_id or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in document_id
        ):
            raise SafeFileError("INVALID_DOCUMENT_ID", "문서 ID 형식이 안전하지 않습니다.")
        async with self._guard:
            local_lock = self._locks.setdefault(document_id, asyncio.Lock())
        async with (
            self._publish_barrier.read(),
            self._file_lock(".wiki-publish.lock", shared=True),
            local_lock,
            self._file_lock(f"{document_id}.lock", shared=False),
        ):
            yield

    @asynccontextmanager
    async def _file_lock(self, filename: str, *, shared: bool) -> AsyncIterator[None]:
        context = file_lock(self._lock_root / filename, shared=shared)
        await asyncio.to_thread(context.__enter__)
        try:
            yield
        finally:
            await asyncio.to_thread(context.__exit__, None, None, None)


class _AsyncReaderWriterLock:
    """Writer-preferring async barrier for source writes and wiki publication."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @asynccontextmanager
    async def read(self) -> AsyncIterator[None]:
        async with self._condition:
            await self._condition.wait_for(lambda: not self._writer and self._waiting_writers == 0)
            self._readers += 1
        try:
            yield
        finally:
            async with self._condition:
                self._readers -= 1
                if self._readers == 0:
                    self._condition.notify_all()

    @asynccontextmanager
    async def write(self) -> AsyncIterator[None]:
        async with self._condition:
            self._waiting_writers += 1
            try:
                await self._condition.wait_for(lambda: not self._writer and self._readers == 0)
                self._writer = True
            finally:
                self._waiting_writers -= 1
        try:
            yield
        finally:
            async with self._condition:
                self._writer = False
                self._condition.notify_all()


class SafeSourceFileStore:
    def __init__(
        self,
        resolver: SourceUriResolver,
        *,
        backup_root: Path,
        max_bytes: int,
        fault_hook: FaultHook | None = None,
        recovery_root: Path | None = None,
    ) -> None:
        self._resolver = resolver
        self._root = resolver.source_root
        self._backup_root = backup_root.resolve()
        self._recovery_root = (
            recovery_root.resolve()
            if recovery_root is not None
            else self._root / ".codegate-recovery"
        )
        self._max_bytes = max_bytes
        self._fault_hook = fault_hook or (lambda _: None)
        self._adapter = ConfinedFileSystem(
            self._root,
            max_bytes=max_bytes,
            fault_hook=lambda stage: self._fault_hook(stage),
        )

    def snapshot(self, source_uri: str) -> FileSnapshot:
        relative = self._resolver.relative_path(source_uri)
        return self._from_adapter(source_uri, self._call(self._adapter.snapshot, relative))

    def is_absent(self, source_uri: str) -> bool:
        relative = self._resolver.relative_path(source_uri)
        return self._call(self._adapter.is_absent, relative)

    def write_backup(self, execution_id: str, snapshot: FileSnapshot) -> Path:
        if not re.fullmatch(r"exec_[0-9a-f]{32}", execution_id):
            raise SafeFileError("INVALID_EXECUTION_ID", "실행 ID 형식이 안전하지 않습니다.")
        backup_directory = self._backup_root / execution_id
        try:
            backup_directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        except FileExistsError as error:
            raise SafeFileError("BACKUP_EXISTS", "실행 백업이 이미 존재합니다.") from error
        backup_path = backup_directory / snapshot.relative_path.name
        adapter_snapshot = AdapterSnapshot(
            relative_path=snapshot.relative_path,
            content=snapshot.content,
            sha256=snapshot.sha256,
            mode=snapshot.mode,
        )
        try:
            return self._call(self._adapter.immutable_backup, backup_path, adapter_snapshot)
        except BaseException:
            backup_directory.rmdir()
            raise

    def read_backup(self, backup_path: str) -> bytes:
        candidate = Path(backup_path)
        try:
            relative = candidate.relative_to(self._backup_root)
        except ValueError as error:
            raise SafeFileError(
                "BACKUP_NOT_FOUND", "검증된 백업 파일을 찾을 수 없습니다."
            ) from error
        if (
            not candidate.is_absolute()
            or len(relative.parts) != 2
            or not re.fullmatch(r"exec_[0-9a-f]{32}", relative.parts[0])
            or relative.parts[1] in {"", ".", ".."}
        ):
            raise SafeFileError("BACKUP_NOT_FOUND", "검증된 백업 파일을 찾을 수 없습니다.")
        adapter = ConfinedFileSystem(self._backup_root, max_bytes=self._max_bytes)
        try:
            return adapter.snapshot(PurePosixPath(*relative.parts)).content
        except FileSystemError as error:
            raise SafeFileError(
                "BACKUP_UNSAFE", "백업 파일을 안전하게 읽을 수 없습니다."
            ) from error

    def existing_backup_path(self, execution_id: str, source_uri: str) -> Path | None:
        relative = self._resolver.relative_path(source_uri)
        candidate = self._backup_root / execution_id / relative.name
        if not candidate.exists():
            return None
        self.read_backup(str(candidate))
        return candidate

    def atomic_replace(
        self,
        *,
        source_uri: str,
        expected_sha256: str,
        new_content: bytes,
    ) -> FileSnapshot:
        relative = self._resolver.relative_path(source_uri)
        snapshot = self._call(
            self._adapter.atomic_replace,
            relative,
            expected_sha256=expected_sha256,
            content=new_content,
        )
        return self._from_adapter(source_uri, snapshot)

    def create_new(self, *, source_uri: str, content: bytes) -> FileSnapshot:
        relative = self._resolver.relative_path(source_uri)
        return self._from_adapter(
            source_uri,
            self._call(self._adapter.create_new, relative, content),
        )

    def move_to_recovery(self, *, source_uri: str, execution_id: str) -> Path:
        if not re.fullmatch(r"exec_[0-9a-f]{32}", execution_id):
            raise SafeFileError("INVALID_EXECUTION_ID", "실행 ID 형식이 안전하지 않습니다.")
        relative = self._resolver.relative_path(source_uri)
        recovery_path = self._recovery_root / execution_id / relative.name
        return self._call(self._adapter.recovery_move, relative, recovery_path)

    def existing_recovery_path(self, execution_id: str, source_uri: str) -> Path | None:
        relative = self._resolver.relative_path(source_uri)
        recovery_relative = PurePosixPath(execution_id, relative.name)
        adapter = ConfinedFileSystem(self._recovery_root, max_bytes=self._max_bytes)
        if adapter.is_absent(recovery_relative):
            return None
        adapter.snapshot(recovery_relative)
        return self._recovery_root.joinpath(*recovery_relative.parts)

    def prune_expired_managed_files(self, *, older_than: datetime) -> list[Path]:
        """Remove only expired app-owned execution directories without following links."""
        if older_than.tzinfo is None:
            raise ValueError("older_than must be timezone-aware")
        removed: list[Path] = []
        for root in {self._backup_root, self._recovery_root}:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            for candidate in root.iterdir():
                if not re.fullmatch(r"exec_[0-9a-f]{32}", candidate.name):
                    continue
                stat = candidate.lstat()
                if _is_reparse_or_link(candidate, stat) or not candidate.is_dir():
                    continue
                modified = datetime.fromtimestamp(stat.st_mtime, UTC)
                if modified >= older_than:
                    continue
                _validate_managed_tree(candidate)
                _remove_managed_tree(candidate)
                removed.append(candidate)
        return removed

    @staticmethod
    def _from_adapter(source_uri: str, snapshot: AdapterSnapshot) -> FileSnapshot:
        return FileSnapshot(
            source_uri=source_uri,
            relative_path=snapshot.relative_path,
            content=snapshot.content,
            sha256=snapshot.sha256,
            mode=snapshot.mode,
        )

    @staticmethod
    def _call(function: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except FileSystemConflict as error:
            raise FileHashConflict() from error
        except FileSystemError as error:
            message = (
                "원본 파일을 안전하게 열 수 없습니다."
                if error.code == "UNSAFE_SOURCE_PATH"
                else str(error)
            )
            raise SafeFileError(error.code, message) from error


def _validate_managed_tree(directory: Path) -> None:
    for entry in os.scandir(directory):
        path = Path(entry.path)
        stat = path.lstat()
        if _is_reparse_or_link(path, stat):
            raise SafeFileError(
                "UNSAFE_RETENTION_PATH",
                "managed retention directory contains a link or reparse point",
            )
        if entry.is_dir(follow_symlinks=False):
            _validate_managed_tree(path)
        elif entry.is_file(follow_symlinks=False):
            continue
        else:
            raise SafeFileError(
                "UNSAFE_RETENTION_PATH",
                "managed retention directory contains an unsupported filesystem entry",
            )


def _remove_managed_tree(directory: Path) -> None:
    for entry in os.scandir(directory):
        path = Path(entry.path)
        stat = path.lstat()
        if _is_reparse_or_link(path, stat):
            raise SafeFileError(
                "UNSAFE_RETENTION_PATH",
                "managed retention directory changed during removal",
            )
        if entry.is_dir(follow_symlinks=False):
            _remove_managed_tree(path)
        elif entry.is_file(follow_symlinks=False):
            path.unlink()
        else:
            raise SafeFileError(
                "UNSAFE_RETENTION_PATH",
                "managed retention directory changed during removal",
            )
    directory.rmdir()


def _is_reparse_or_link(path: Path, stat: os.stat_result) -> bool:
    file_attributes = int(getattr(stat, "st_file_attributes", 0))
    return path.is_symlink() or bool(file_attributes & 0x400)
