from __future__ import annotations

import hashlib
import os
import secrets
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4


class FileSystemError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FileSystemConflict(FileSystemError):
    def __init__(self, message: str = "source changed after the plan was prepared") -> None:
        super().__init__("SOURCE_HASH_CONFLICT", message)


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    relative_path: PurePosixPath
    content: bytes
    sha256: str
    mode: int


FaultHook = Callable[[str], None]


class ConfinedFileSystem:
    """Read and mutate regular files below one resolved root."""

    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int,
        fault_hook: FaultHook | None = None,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.max_bytes = max_bytes
        self._fault_hook = fault_hook or (lambda _: None)

    def snapshot(self, relative_path: PurePosixPath) -> FileSnapshot:
        relative = _validate_relative(relative_path)
        if os.name == "nt":
            content, mode = self._windows_read(relative)
        else:
            content, mode = self._posix_read(relative)
        return FileSnapshot(relative, content, _sha256(content), mode)

    def is_absent(self, relative_path: PurePosixPath) -> bool:
        """Return true only for a missing leaf; unsafe links and file types still fail closed."""

        relative = _validate_relative(relative_path)
        if os.name == "nt":
            target = self._windows_confined_path(relative, allow_missing_leaf=True)
            if not target.exists() and not target.is_symlink():
                return True
            _reject_reparse(target)
            if not target.is_file():
                raise FileSystemError("UNSUPPORTED_FILE_TYPE", "only regular files are supported")
            return False
        with self._open_posix_parent(relative) as parent_fd:
            try:
                file_stat = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return True
            if not stat.S_ISREG(file_stat.st_mode):
                raise FileSystemError("UNSUPPORTED_FILE_TYPE", "only regular files are supported")
            return False

    def immutable_backup(self, destination: Path, snapshot: FileSnapshot) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _reject_reparse_chain(destination.parent)
        self._write_new_path(destination, snapshot.content, 0o600)
        fsync_directory(destination.parent)
        self._fault_hook("backup_fsynced")
        return destination

    def atomic_replace(
        self,
        relative_path: PurePosixPath,
        *,
        expected_sha256: str,
        content: bytes,
    ) -> FileSnapshot:
        self._check_size(content, result=True)
        relative = _validate_relative(relative_path)
        before = self.snapshot(relative)
        if not secrets.compare_digest(before.sha256, expected_sha256):
            raise FileSystemConflict()
        if os.name == "nt":
            self._windows_replace(relative, expected_sha256, content, before.mode)
        else:
            self._posix_replace(relative, expected_sha256, content, before.mode)
        after = self.snapshot(relative)
        if after.content != content:
            raise FileSystemError("ATOMIC_WRITE_VERIFY_FAILED", "atomic write verification failed")
        return after

    def create_new(
        self, relative_path: PurePosixPath, content: bytes, *, mode: int = 0o600
    ) -> FileSnapshot:
        self._check_size(content, result=True)
        relative = _validate_relative(relative_path)
        target = self.root.joinpath(*relative.parts)
        self._ensure_confined_parent(relative)
        temporary = target.parent / f".codegate-{uuid4().hex}.tmp"
        self._write_new_path(temporary, content, mode)
        self._fault_hook("temp_fsynced")
        try:
            if os.name == "nt":
                _windows_move_new(temporary, target)
            else:
                os.link(temporary, target, follow_symlinks=False)
                temporary.unlink()
                fsync_directory(target.parent)
            self._fault_hook("created")
        except FileExistsError as error:
            raise FileSystemError("TARGET_EXISTS", "target already exists") from error
        except OSError as error:
            if target.exists():
                raise FileSystemError("TARGET_EXISTS", "target already exists") from error
            raise
        finally:
            temporary.unlink(missing_ok=True)
        return self.snapshot(relative)

    def recovery_move(self, relative_path: PurePosixPath, recovery_path: Path) -> Path:
        relative = _validate_relative(relative_path)
        source = self._windows_confined_path(relative)
        recovery_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _reject_reparse_chain(recovery_path.parent)
        if source.stat().st_dev != recovery_path.parent.stat().st_dev:
            raise FileSystemError(
                "RECOVERY_VOLUME_MISMATCH", "recovery must be on the source volume"
            )
        if os.name == "nt":
            _windows_move_new(source, recovery_path)
        else:
            os.link(source, recovery_path, follow_symlinks=False)
            source.unlink()
            fsync_directory(source.parent)
            fsync_directory(recovery_path.parent)
        return recovery_path

    def _check_size(self, content: bytes, *, result: bool = False) -> None:
        if len(content) > self.max_bytes:
            code = "RESULT_TOO_LARGE" if result else "FILE_TOO_LARGE"
            raise FileSystemError(code, "file size exceeds the configured limit")

    def _posix_read(self, relative: PurePosixPath) -> tuple[bytes, int]:
        with self._open_posix_parent(relative) as parent_fd:
            descriptor = _open_posix_regular(parent_fd, relative.name)
            try:
                file_stat = os.fstat(descriptor)
                return self._read_posix_descriptor(descriptor, file_stat.st_size), stat.S_IMODE(
                    file_stat.st_mode
                )
            finally:
                os.close(descriptor)

    def _posix_replace(
        self,
        relative: PurePosixPath,
        expected_sha256: str,
        content: bytes,
        mode: int,
    ) -> None:
        with self._open_posix_parent(relative) as parent_fd:
            temporary = f".codegate-{uuid4().hex}.tmp"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_fd,
            )
            try:
                _write_all(descriptor, content)
                os.fchmod(descriptor, mode)  # type: ignore[attr-defined]
                os.fsync(descriptor)
                self._fault_hook("temp_fsynced")
            finally:
                os.close(descriptor)
            try:
                recheck = _open_posix_regular(parent_fd, relative.name)
                try:
                    current = self._read_posix_descriptor(recheck, os.fstat(recheck).st_size)
                finally:
                    os.close(recheck)
                if not secrets.compare_digest(_sha256(current), expected_sha256):
                    raise FileSystemConflict()
                os.replace(temporary, relative.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                self._fault_hook("replaced")
                os.fsync(parent_fd)
                self._fault_hook("directory_fsynced")
            except BaseException:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=parent_fd)
                raise

    @contextmanager
    def _open_posix_parent(self, relative: PurePosixPath) -> Iterator[int]:
        descriptors: list[int] = []
        root_fd = os.open(
            self.root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        descriptors.append(root_fd)
        current_fd = root_fd
        try:
            for component in relative.parts[:-1]:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current_fd,
                )
                descriptors.append(next_fd)
                current_fd = next_fd
            yield current_fd
        except OSError as error:
            raise FileSystemError("UNSAFE_SOURCE_PATH", "source path is unsafe") from error
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _read_posix_descriptor(self, descriptor: int, size: int) -> bytes:
        if size > self.max_bytes:
            raise FileSystemError("FILE_TOO_LARGE", "file size exceeds the configured limit")
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = self.max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        self._check_size(content)
        return content

    def _windows_confined_path(
        self, relative: PurePosixPath, *, allow_missing_leaf: bool = False
    ) -> Path:
        candidate = self.root.joinpath(*relative.parts)
        existing_parent = candidate.parent
        while not existing_parent.exists() and existing_parent != self.root:
            existing_parent = existing_parent.parent
        _reject_reparse_chain(existing_parent, stop=self.root)
        if not allow_missing_leaf:
            _reject_reparse(candidate)
        return candidate

    def _ensure_confined_parent(self, relative: PurePosixPath) -> Path:
        current = self.root
        _reject_reparse(current)
        for component in relative.parts[:-1]:
            current = current / component
            with suppress(FileExistsError):
                current.mkdir(mode=0o700)
            _reject_reparse(current)
            if not current.is_dir():
                raise FileSystemError("UNSUPPORTED_FILE_TYPE", "file parent must be a directory")
        return current

    def _windows_read(self, relative: PurePosixPath) -> tuple[bytes, int]:
        import win32con  # type: ignore[import-untyped]
        import win32file  # type: ignore[import-untyped]

        path = self._windows_confined_path(relative)
        handle = win32file.CreateFile(
            str(path),
            win32con.GENERIC_READ,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
            None,
            win32con.OPEN_EXISTING,
            win32con.FILE_ATTRIBUTE_NORMAL | 0x00200000,
            None,
        )
        try:
            info = win32file.GetFileInformationByHandle(handle)
            if info[0] & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
                raise FileSystemError("UNSAFE_SOURCE_PATH", "source path is a reparse point")
            size = (int(info[5]) << 32) | int(info[6])
            if size > self.max_bytes:
                raise FileSystemError("FILE_TOO_LARGE", "file size exceeds the configured limit")
            chunks: list[bytes] = []
            remaining = self.max_bytes + 1
            while remaining:
                _, chunk = win32file.ReadFile(handle, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
            self._check_size(content)
            return content, stat.S_IMODE(path.stat().st_mode)
        finally:
            handle.Close()

    def _windows_replace(
        self,
        relative: PurePosixPath,
        expected_sha256: str,
        content: bytes,
        mode: int,
    ) -> None:
        target = self._windows_confined_path(relative)
        temporary = target.parent / f".codegate-{uuid4().hex}.tmp"
        self._write_new_path(temporary, content, mode)
        self._fault_hook("temp_fsynced")
        try:
            current, _ = self._windows_read(relative)
            if not secrets.compare_digest(_sha256(current), expected_sha256):
                raise FileSystemConflict()
            _windows_replace_file(target, temporary)
            self._fault_hook("replaced")
            self._fault_hook("directory_fsynced")
        finally:
            temporary.unlink(missing_ok=True)

    def _write_new_path(self, path: Path, content: bytes, mode: int) -> None:
        if os.name == "nt":
            import win32con
            import win32file

            handle = win32file.CreateFile(
                str(path),
                win32con.GENERIC_WRITE,
                win32con.FILE_SHARE_READ,
                None,
                win32con.CREATE_NEW,
                win32con.FILE_ATTRIBUTE_NORMAL,
                None,
            )
            try:
                offset = 0
                while offset < len(content):
                    _, written = win32file.WriteFile(handle, content[offset : offset + 65_536])
                    offset += int(written)
                win32file.FlushFileBuffers(handle)
            finally:
                handle.Close()
            return
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        try:
            _write_all(descriptor, content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _validate_relative(path: PurePosixPath) -> PurePosixPath:
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise FileSystemError("UNSAFE_SOURCE_PATH", "path must be a confined relative path")
    return path


def _open_posix_regular(parent_fd: int, name: str) -> int:
    descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise FileSystemError("UNSUPPORTED_FILE_TYPE", "only regular files are supported")
    return descriptor


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    offset = 0
    while offset < len(view):
        offset += os.write(descriptor, view[offset:])


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _reject_reparse(path: Path) -> None:
    if os.name != "nt":
        if path.is_symlink():
            raise FileSystemError("UNSAFE_SOURCE_PATH", "symlink paths are forbidden")
        return
    import win32api  # type: ignore[import-untyped]
    import win32con

    try:
        attributes = win32api.GetFileAttributes(str(path))
    except OSError as error:
        raise FileSystemError("UNSAFE_SOURCE_PATH", "source path is unavailable") from error
    if attributes & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
        raise FileSystemError("UNSAFE_SOURCE_PATH", "reparse-point paths are forbidden")


def _reject_reparse_chain(path: Path, *, stop: Path | None = None) -> None:
    resolved_stop = stop.resolve() if stop is not None else None
    current = path
    checked: list[Path] = []
    while True:
        checked.append(current)
        if resolved_stop is not None and current == resolved_stop:
            break
        if current.parent == current:
            break
        current = current.parent
    for candidate in reversed(checked):
        _reject_reparse(candidate)


def fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_file(path: Path) -> None:
    if os.name == "nt":
        import win32con
        import win32file

        handle = win32file.CreateFile(
            str(path),
            win32con.GENERIC_WRITE,
            win32con.FILE_SHARE_READ,
            None,
            win32con.OPEN_EXISTING,
            win32con.FILE_ATTRIBUTE_NORMAL | 0x00200000,
            None,
        )
        try:
            win32file.FlushFileBuffers(handle)
        finally:
            handle.Close()
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def restrict_to_current_user(path: Path, *, directory: bool = False) -> None:
    if os.name != "nt":
        path.chmod(0o700 if directory else 0o600)
        return
    import ntsecuritycon  # type: ignore[import-untyped]
    import win32api
    import win32con
    import win32security  # type: ignore[import-untyped]

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        current_user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    system = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    inheritance = 0
    if directory:
        inheritance = ntsecuritycon.CONTAINER_INHERIT_ACE | ntsecuritycon.OBJECT_INHERIT_ACE
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAceEx(
        win32security.ACL_REVISION_DS,
        inheritance,
        ntsecuritycon.FILE_ALL_ACCESS,
        current_user,
    )
    dacl.AddAccessAllowedAceEx(
        win32security.ACL_REVISION_DS,
        inheritance,
        ntsecuritycon.FILE_ALL_ACCESS,
        system,
    )
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )


def _windows_replace_file(target: Path, replacement: Path) -> None:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    replace_file = kernel32.ReplaceFileW
    replace_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    replace_file.restype = ctypes.c_int
    replacefile_write_through = 0x00000001
    if not replace_file(str(target), str(replacement), None, replacefile_write_through, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_move_new(source: Path, target: Path) -> None:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file = kernel32.MoveFileExW
    move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
    move_file.restype = ctypes.c_int
    movefile_write_through = 0x00000008
    if not move_file(str(source), str(target), movefile_write_through):
        error = ctypes.get_last_error()
        if error in {80, 183}:
            raise FileExistsError(str(target))
        raise ctypes.WinError(error)
