from __future__ import annotations

import os
import shutil
from uuid import uuid4

from codegate_api.config import Settings


class RuntimeWorkspace:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._source_root = settings.resolved_source_root()
        self._seed_source_root = settings.resolved_seed_source_root()

    def bootstrap(self) -> None:
        if self._settings.environment == "production":
            self._verify_persistent_volume()
        if self._source_root.is_dir():
            return
        if not self._settings.bootstrap_demo:
            raise RuntimeError(f"source root is missing: {self._source_root}")
        self._copy_seed_source()

    def reset_source(self) -> None:
        if self._settings.environment == "production":
            raise RuntimeError("demo reset is forbidden in production")
        if self._source_root.exists():
            if not self._source_root.is_relative_to(self._settings.resolved_runtime_root()):
                raise RuntimeError("refusing to reset a source root outside runtime_root")
            shutil.rmtree(self._source_root)
        self._copy_seed_source()

    def _copy_seed_source(self) -> None:
        if not self._seed_source_root.is_dir():
            raise RuntimeError(f"source seed is missing: {self._seed_source_root}")
        self._source_root.parent.mkdir(parents=True, exist_ok=True)
        staging = self._source_root.parent / f".source-seed-{uuid4().hex}"
        shutil.copytree(self._seed_source_root, staging, copy_function=shutil.copy2)
        os.replace(staging, self._source_root)
        descriptor = os.open(self._source_root.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _verify_persistent_volume(self) -> None:
        mount = self._settings.railway_volume_mount_path
        if mount is None:
            raise RuntimeError("production persistent volume is not configured")
        resolved_mount = mount.expanduser().resolve()
        if not resolved_mount.is_dir():
            raise RuntimeError(f"persistent volume mount is missing: {resolved_mount}")
        probe = resolved_mount / f".codegate-write-probe-{uuid4().hex}"
        try:
            descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, b"ok")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            probe.unlink()
        except OSError as error:
            raise RuntimeError("persistent volume mount is not writable") from error
