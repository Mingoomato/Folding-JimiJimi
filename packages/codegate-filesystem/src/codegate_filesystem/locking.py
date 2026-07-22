from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def file_lock(path: Path, *, shared: bool = False) -> Iterator[None]:
    """Acquire a process-wide advisory lock on one lock file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        with _windows_file_lock(path, shared=shared):
            yield
        return
    with _posix_file_lock(path, shared=shared):
        yield


@contextmanager
def _posix_file_lock(path: Path, *, shared: bool) -> Iterator[None]:
    import fcntl

    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(  # type: ignore[attr-defined]
            descriptor,
            fcntl.LOCK_SH if shared else fcntl.LOCK_EX,  # type: ignore[attr-defined]
        )
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)  # type: ignore[attr-defined]
        os.close(descriptor)


@contextmanager
def _windows_file_lock(path: Path, *, shared: bool) -> Iterator[None]:
    import pywintypes  # type: ignore[import-untyped]
    import win32con  # type: ignore[import-untyped]
    import win32file  # type: ignore[import-untyped]

    handle = win32file.CreateFile(
        str(path),
        win32con.GENERIC_READ | win32con.GENERIC_WRITE,
        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
        None,
        win32con.OPEN_ALWAYS,
        win32con.FILE_ATTRIBUTE_NORMAL,
        None,
    )
    overlapped = pywintypes.OVERLAPPED()
    flags = 0 if shared else win32con.LOCKFILE_EXCLUSIVE_LOCK
    acquired = False
    try:
        win32file.LockFileEx(handle, flags, 0, 1, overlapped)
        acquired = True
        yield
    finally:
        if acquired:
            win32file.UnlockFileEx(handle, 0, 1, overlapped)
        handle.Close()
