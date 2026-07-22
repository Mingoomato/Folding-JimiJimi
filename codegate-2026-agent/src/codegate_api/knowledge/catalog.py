from __future__ import annotations

import os
import re
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from codegate_filesystem import file_lock, fsync_directory, fsync_file

from codegate_api.files.resolver import SourceUriResolver
from codegate_api.knowledge.repository import KnowledgeRepository


class KnowledgeCatalogError(RuntimeError):
    pass


class KnowledgeCatalog:
    """Manage immutable packages behind an atomic active-release pointer."""

    def __init__(
        self,
        *,
        seed_root: Path,
        releases_root: Path,
        pointer_path: Path,
        source_resolver: SourceUriResolver,
    ) -> None:
        self._seed_root = seed_root.resolve()
        self._releases_root = releases_root.resolve()
        self._pointer_path = pointer_path.resolve()
        self._source_resolver = source_resolver
        self._thread_lock = threading.RLock()
        self._repository: KnowledgeRepository | None = None

    def bootstrap(
        self,
        *,
        allow_stale_sources: bool = False,
        replace_invalid_demo_seed: bool = False,
        allow_seed_bootstrap: bool = True,
    ) -> None:
        self._releases_root.mkdir(parents=True, exist_ok=True)
        self._pointer_path.parent.mkdir(parents=True, exist_ok=True)
        with self._exclusive_catalog_lock(), self._thread_lock:
            if not self._pointer_path.is_file():
                if not allow_seed_bootstrap:
                    raise KnowledgeCatalogError(
                        "active knowledge release must be provisioned before startup"
                    )
                release_name = self._copy_seed_release()
                self._write_pointer(release_name)
            try:
                self._repository = self._load_pointer_repository(
                    validate_sources=not allow_stale_sources
                )
            except (KnowledgeCatalogError, RuntimeError):
                if not replace_invalid_demo_seed:
                    raise
                release_name = self._copy_seed_release(unique=True)
                self._write_pointer(release_name)
                self._repository = self._load_pointer_repository()

    def snapshot(self) -> KnowledgeRepository:
        with self._thread_lock:
            if self._repository is None:
                raise KnowledgeCatalogError("knowledge catalog has not been bootstrapped")
            return self._repository

    def refresh(self) -> KnowledgeRepository:
        with self._thread_lock:
            self._repository = self._load_pointer_repository()
            return self._repository

    def validate_candidate(self, candidate: Path) -> None:
        repository = KnowledgeRepository(candidate.resolve(), self._source_resolver)
        repository.load()

    def stage(self, expected_parent_version: str) -> Path:
        with self._thread_lock:
            repository = self.snapshot()
            if repository.version != expected_parent_version:
                raise KnowledgeCatalogError("active knowledge version changed before staging")
            staging_root = self._releases_root.parent / "staging"
            staging_root.mkdir(parents=True, exist_ok=True)
            candidate = staging_root / f"candidate-{uuid4().hex}"
            shutil.copytree(repository.package_root, candidate, copy_function=shutil.copy2)
            return candidate

    def publish(
        self,
        candidate: Path,
        *,
        expected_parent_version: str,
        candidate_version: str,
    ) -> bool:
        candidate = candidate.resolve()
        if not candidate.is_dir() or not candidate.is_relative_to(self._releases_root.parent):
            raise KnowledgeCatalogError("candidate is outside the knowledge runtime root")
        validated = KnowledgeRepository(candidate, self._source_resolver)
        validated.load()
        if validated.version != candidate_version:
            raise KnowledgeCatalogError("candidate VERSION does not match publish request")

        release_name = _release_name(candidate_version)
        destination = self._releases_root / release_name
        with self._exclusive_catalog_lock(), self._thread_lock:
            if self._read_pointer_version() != expected_parent_version:
                return False
            if destination.exists():
                existing = KnowledgeRepository(destination, self._source_resolver)
                existing.load()
                if existing.version != candidate_version:
                    raise KnowledgeCatalogError("knowledge release name collision")
                self.discard(candidate)
            else:
                _fsync_tree(candidate)
                staging_parent = candidate.parent
                os.replace(candidate, destination)
                _fsync_directory(self._releases_root)
                if staging_parent != self._releases_root:
                    _fsync_directory(staging_parent)
            published = KnowledgeRepository(destination, self._source_resolver)
            published.load()
            self._write_pointer(release_name)
            self._repository = published
        return True

    def discard(self, candidate: Path) -> None:
        resolved = candidate.resolve()
        staging_root = (self._releases_root.parent / "staging").resolve()
        if resolved.is_dir() and resolved.is_relative_to(staging_root):
            shutil.rmtree(resolved)

    def reset_to_seed(self) -> str:
        with self._exclusive_catalog_lock(), self._thread_lock:
            release_name = self._copy_seed_release(unique=True)
            self._write_pointer(release_name)
            self._repository = self._load_pointer_repository()
            return self._repository.version

    def _copy_seed_release(self, *, unique: bool = False) -> str:
        if not self._seed_root.is_dir():
            raise KnowledgeCatalogError(f"knowledge seed is missing: {self._seed_root}")
        version = (self._seed_root / "VERSION").read_text(encoding="utf-8").strip()
        suffix = f"-{uuid4().hex[:8]}" if unique else ""
        release_name = f"{_release_name(version)}{suffix}"
        destination = self._releases_root / release_name
        if destination.exists():
            return release_name
        staging = self._releases_root.parent / f"seed-{uuid4().hex}"
        shutil.copytree(self._seed_root, staging, copy_function=shutil.copy2)
        seed_repository = KnowledgeRepository(staging, self._source_resolver)
        seed_repository.load()
        _fsync_tree(staging)
        os.replace(staging, destination)
        _fsync_directory(self._releases_root)
        _fsync_directory(staging.parent)
        return release_name

    def _load_pointer_repository(
        self,
        *,
        validate_sources: bool = True,
    ) -> KnowledgeRepository:
        release_path = self._read_pointer_release_path()
        repository = KnowledgeRepository(release_path, self._source_resolver)
        repository.load(validate_sources=validate_sources)
        return repository

    def _read_pointer_version(self) -> str:
        release_path = self._read_pointer_release_path()
        version = (release_path / "VERSION").read_text(encoding="utf-8").strip()
        if not version:
            raise KnowledgeCatalogError("active knowledge VERSION is empty")
        return version

    def _read_pointer_release_path(self) -> Path:
        try:
            release_name = self._pointer_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError as error:
            raise KnowledgeCatalogError("active knowledge pointer is missing") from error
        if not release_name or release_name != Path(release_name).name:
            raise KnowledgeCatalogError("active knowledge pointer is invalid")
        release_path = (self._releases_root / release_name).resolve()
        if not release_path.is_relative_to(self._releases_root) or not release_path.is_dir():
            raise KnowledgeCatalogError("active knowledge release is missing")
        return release_path

    def _write_pointer(self, release_name: str) -> None:
        self._pointer_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._pointer_path.parent / f".CURRENT-{uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            payload = f"{release_name}\n".encode()
            view = memoryview(payload)
            written = 0
            while written < len(view):
                written += os.write(descriptor, view[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, self._pointer_path)
        _fsync_directory(self._pointer_path.parent)

    @contextmanager
    def _exclusive_catalog_lock(self):  # type: ignore[no-untyped-def]
        lock_path = self._pointer_path.parent / "catalog.lock"
        with file_lock(lock_path):
            yield


def _release_name(version: str) -> str:
    sanitized = re.sub(r"[^0-9A-Za-z._-]+", "-", version).strip("-.")
    if not sanitized:
        raise KnowledgeCatalogError("knowledge version cannot form a safe release name")
    return sanitized


def _fsync_directory(path: Path) -> None:
    fsync_directory(path)


def _fsync_tree(root: Path) -> None:
    directories = [root]
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise KnowledgeCatalogError("knowledge candidate must not contain symlinks")
        if path.is_dir():
            directories.append(path)
            continue
        if not path.is_file():
            raise KnowledgeCatalogError("knowledge candidate contains a non-regular file")
        fsync_file(path)
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _fsync_directory(directory)
