from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import re
import stat
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

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


class DocumentLockManager:
    """Combine an in-process lock with POSIX flock for same-host workers."""

    def __init__(self, lock_root: Path) -> None:
        self._lock_root = lock_root.resolve()
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()
        self._publish_barrier = _AsyncReaderWriterLock()

    @asynccontextmanager
    async def acquire_publish(self) -> AsyncIterator[None]:
        """Exclude all source writes while publishing one validated wiki build."""
        async with (
            self._publish_barrier.write(),
            self._file_lock(".wiki-publish.lock", fcntl.LOCK_EX),
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
            self._file_lock(".wiki-publish.lock", fcntl.LOCK_SH),
            local_lock,
            self._file_lock(f"{document_id}.lock", fcntl.LOCK_EX),
        ):
            yield

    @asynccontextmanager
    async def _file_lock(self, filename: str, operation: int) -> AsyncIterator[None]:
        self._lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self._lock_root / filename, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            await asyncio.to_thread(fcntl.flock, descriptor, operation)
            yield
        finally:
            await asyncio.to_thread(fcntl.flock, descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


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
    ) -> None:
        self._resolver = resolver
        self._root = resolver.source_root
        self._backup_root = backup_root.resolve()
        self._max_bytes = max_bytes
        self._fault_hook = fault_hook or (lambda _: None)

    def snapshot(self, source_uri: str) -> FileSnapshot:
        relative = self._resolver.relative_path(source_uri)
        with self._open_parent(relative) as parent_fd:
            descriptor = self._open_regular_file(parent_fd, relative.name)
            try:
                file_stat = os.fstat(descriptor)
                content = self._read_descriptor(descriptor, file_stat.st_size)
            finally:
                os.close(descriptor)
        return FileSnapshot(
            source_uri=source_uri,
            relative_path=relative,
            content=content,
            sha256=_sha256(content),
            mode=stat.S_IMODE(file_stat.st_mode),
        )

    def write_backup(self, execution_id: str, snapshot: FileSnapshot) -> Path:
        if not re.fullmatch(r"exec_[0-9a-f]{32}", execution_id):
            raise SafeFileError("INVALID_EXECUTION_ID", "실행 ID 형식이 안전하지 않습니다.")
        backup_root_existed = self._backup_root.is_dir()
        self._backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not backup_root_existed:
            _fsync_directory(self._backup_root.parent)
        backup_directory = self._backup_root / execution_id
        backup_directory.mkdir(exist_ok=False, mode=0o700)
        filename = snapshot.relative_path.name
        backup_path = backup_directory / filename
        descriptor = os.open(
            backup_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _o_nofollow(),
            0o600,
        )
        try:
            _write_all(descriptor, snapshot.content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(backup_directory)
        _fsync_directory(self._backup_root)
        self._fault_hook("backup_fsynced")
        return backup_path

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
        try:
            root_fd = os.open(
                self._backup_root,
                os.O_RDONLY | os.O_DIRECTORY | _o_nofollow(),
            )
            try:
                directory_fd = os.open(
                    relative.parts[0],
                    os.O_RDONLY | os.O_DIRECTORY | _o_nofollow(),
                    dir_fd=root_fd,
                )
                try:
                    descriptor = self._open_regular_file(directory_fd, relative.parts[1])
                    try:
                        file_stat = os.fstat(descriptor)
                        return self._read_descriptor(descriptor, file_stat.st_size)
                    finally:
                        os.close(descriptor)
                finally:
                    os.close(directory_fd)
            finally:
                os.close(root_fd)
        except SafeFileError:
            raise
        except OSError as error:
            raise SafeFileError("BACKUP_UNSAFE", "백업 파일을 안전하게 열 수 없습니다.") from error

    def atomic_replace(
        self,
        *,
        source_uri: str,
        expected_sha256: str,
        new_content: bytes,
    ) -> FileSnapshot:
        if len(new_content) > self._max_bytes:
            raise SafeFileError("FILE_TOO_LARGE", "수정 결과가 허용된 파일 크기를 초과합니다.")
        relative = self._resolver.relative_path(source_uri)
        with self._open_parent(relative) as parent_fd:
            current_fd = self._open_regular_file(parent_fd, relative.name)
            try:
                current_stat = os.fstat(current_fd)
                current = self._read_descriptor(current_fd, current_stat.st_size)
            finally:
                os.close(current_fd)
            if not _constant_hash_equal(_sha256(current), expected_sha256):
                raise FileHashConflict()

            temp_name = f".codegate-{uuid4().hex}.tmp"
            temp_fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _o_nofollow(),
                0o600,
                dir_fd=parent_fd,
            )
            try:
                _write_all(temp_fd, new_content)
                os.fchmod(temp_fd, stat.S_IMODE(current_stat.st_mode))
                os.fsync(temp_fd)
                self._fault_hook("temp_fsynced")
            finally:
                os.close(temp_fd)

            try:
                recheck_fd = self._open_regular_file(parent_fd, relative.name)
                try:
                    recheck_stat = os.fstat(recheck_fd)
                    recheck_content = self._read_descriptor(recheck_fd, recheck_stat.st_size)
                finally:
                    os.close(recheck_fd)
                if not _constant_hash_equal(_sha256(recheck_content), expected_sha256):
                    raise FileHashConflict()
                os.replace(
                    temp_name,
                    relative.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                self._fault_hook("replaced")
                os.fsync(parent_fd)
                self._fault_hook("directory_fsynced")
            except BaseException:
                with suppress(FileNotFoundError):
                    os.unlink(temp_name, dir_fd=parent_fd)
                raise

            final_fd = self._open_regular_file(parent_fd, relative.name)
            try:
                final_stat = os.fstat(final_fd)
                final_content = self._read_descriptor(final_fd, final_stat.st_size)
            finally:
                os.close(final_fd)
        if final_content != new_content:
            raise SafeFileError("ATOMIC_WRITE_VERIFY_FAILED", "원자 쓰기 검증에 실패했습니다.")
        return FileSnapshot(
            source_uri=source_uri,
            relative_path=relative,
            content=final_content,
            sha256=_sha256(final_content),
            mode=stat.S_IMODE(final_stat.st_mode),
        )

    @contextmanager
    def _open_parent(self, relative: PurePosixPath) -> Iterator[int]:
        descriptors: list[int] = []
        root_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | _o_nofollow())
        descriptors.append(root_fd)
        current_fd = root_fd
        try:
            try:
                for component in relative.parts[:-1]:
                    next_fd = os.open(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | _o_nofollow(),
                        dir_fd=current_fd,
                    )
                    descriptors.append(next_fd)
                    current_fd = next_fd
            except OSError as error:
                raise SafeFileError(
                    "UNSAFE_SOURCE_PATH", "원본 경로를 안전하게 열 수 없습니다."
                ) from error
            yield current_fd
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _open_regular_file(self, parent_fd: int, filename: str) -> int:
        try:
            descriptor = os.open(filename, os.O_RDONLY | _o_nofollow(), dir_fd=parent_fd)
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                os.close(descriptor)
                raise SafeFileError("UNSUPPORTED_FILE_TYPE", "일반 파일만 수정할 수 있습니다.")
            return descriptor
        except SafeFileError:
            raise
        except OSError as error:
            raise SafeFileError(
                "UNSAFE_SOURCE_PATH", "원본 파일을 안전하게 열 수 없습니다."
            ) from error

    def _read_descriptor(self, descriptor: int, size: int) -> bytes:
        if size > self._max_bytes:
            raise SafeFileError("FILE_TOO_LARGE", "원본 파일 크기 제한을 초과했습니다.")
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = self._max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > self._max_bytes:
            raise SafeFileError("FILE_TOO_LARGE", "원본 파일 크기 제한을 초과했습니다.")
        return content


def _o_nofollow() -> int:
    return getattr(os, "O_NOFOLLOW", 0)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    written = 0
    while written < len(view):
        written += os.write(descriptor, view[written:])


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | _o_nofollow())
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _constant_hash_equal(left: str, right: str) -> bool:
    import secrets

    return secrets.compare_digest(left, right)
