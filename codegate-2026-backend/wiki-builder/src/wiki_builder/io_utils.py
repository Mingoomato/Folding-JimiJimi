from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from codegate_filesystem import file_lock, fsync_directory

from wiki_builder.errors import UnsafeOutputError

GENERATOR_MARKER = ".generated-by-wiki-builder"
CURRENT_NOT_CHECKED = object()


def ensure_safe_output_path(output_dir: Path, project_dir: Path) -> None:
    output = output_dir.resolve()
    workspace = project_dir.parent.resolve()
    if output.name != "llm-wiki" or output.parent != workspace:
        raise UnsafeOutputError(f"출력 경로는 작업공간 바로 아래의 llm-wiki여야 합니다: {output}")
    if output.is_symlink():
        raise UnsafeOutputError(f"심볼릭 링크 출력 경로는 사용할 수 없습니다: {output}")


def create_staging_dir(output_dir: Path, project_dir: Path) -> Path:
    ensure_safe_output_path(output_dir, project_dir)
    return Path(tempfile.mkdtemp(prefix=".llm-wiki-build-", dir=output_dir.parent))


def install_staging_dir(staging_dir: Path, output_dir: Path, project_dir: Path) -> None:
    ensure_safe_output_path(output_dir, project_dir)
    marker = output_dir / GENERATOR_MARKER
    if output_dir.exists():
        if not marker.is_file():
            raise UnsafeOutputError(
                f"기존 출력 폴더에 빌더 표식이 없어 덮어쓰지 않습니다: {output_dir}"
            )
        shutil.rmtree(output_dir)
    os.replace(staging_dir, output_dir)


def ensure_scoped_storage_path(path: Path, storage_root: Path) -> None:
    resolved = path.resolve()
    root = storage_root.resolve()
    if root == resolved or root not in resolved.parents:
        raise UnsafeOutputError(f"저장 경로가 storage_root 밖에 있습니다: {resolved}")
    current = root
    for part in resolved.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise UnsafeOutputError(f"심볼릭 링크 저장 경로는 사용할 수 없습니다: {current}")


def create_build_staging_dir(builds_dir: Path, storage_root: Path) -> Path:
    ensure_scoped_storage_path(builds_dir, storage_root)
    builds_dir.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=".build-staging-", dir=builds_dir))


def _file_map(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def install_immutable_build(
    staging_dir: Path,
    target_dir: Path,
    storage_root: Path,
) -> bool:
    ensure_scoped_storage_path(target_dir, storage_root)
    with _file_lock(target_dir.parent / ".install.lock"):
        if target_dir.exists():
            if not (target_dir / GENERATOR_MARKER).is_file():
                raise UnsafeOutputError(f"기존 빌드에 생성기 표식이 없습니다: {target_dir}")
            if _file_map(staging_dir) != _file_map(target_dir):
                raise UnsafeOutputError(f"같은 build_id의 내용이 다릅니다: {target_dir.name}")
            discard_staging_dir(staging_dir)
            return False
        os.replace(staging_dir, target_dir)
        return True


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, separators=(",", ": "))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def _file_lock(lock_path: Path):
    with file_lock(lock_path):
        yield


def activate_build(
    wiki_root: Path,
    storage_root: Path,
    pointer: dict[str, Any],
    actor_id: str,
    expected_current_build_id: str | None | object = CURRENT_NOT_CHECKED,
    before_write: Callable[[], None] | None = None,
) -> None:
    ensure_scoped_storage_path(wiki_root, storage_root)
    current_path = wiki_root / "current.json"
    with _file_lock(wiki_root / ".activation.lock"):
        if expected_current_build_id is not CURRENT_NOT_CHECKED:
            actual_current_build_id: str | None = None
            if current_path.is_file():
                try:
                    current = json.loads(current_path.read_text(encoding="utf-8"))
                    actual_current_build_id = current.get("build_id")
                except (json.JSONDecodeError, AttributeError) as exc:
                    raise UnsafeOutputError(
                        f"current.json을 읽을 수 없어 조건부 활성화를 중단합니다: {current_path}"
                    ) from exc
            if actual_current_build_id != expected_current_build_id:
                raise UnsafeOutputError(
                    "현재 빌드가 요청 시작 시점과 달라 활성화하지 않습니다: "
                    f"expected={expected_current_build_id!r}, "
                    f"actual={actual_current_build_id!r}"
                )

        if before_write is not None:
            before_write()

        atomic_write_json(current_path, pointer)
        audit_dir = wiki_root / "audit" / "builds"
        audit_dir.mkdir(parents=True, exist_ok=True)
        activated_at = datetime.now(UTC)
        event_id = f"{activated_at.strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid4().hex[:12]}"
        atomic_write_json(
            audit_dir / f"{event_id}.json",
            {
                **pointer,
                "event_id": event_id,
                "event": "build_activated",
                "actor_id": actor_id,
                "activated_at": activated_at.isoformat(),
            },
        )


def discard_staging_dir(staging_dir: Path) -> None:
    if staging_dir.exists() and staging_dir.name.startswith(
        (".llm-wiki-build-", ".build-staging-")
    ):
        shutil.rmtree(staging_dir)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_json(path: Path, value: Any) -> None:
    write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, separators=(",", ": ")) + "\n",
    )


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    lines = [json.dumps(record, ensure_ascii=False, separators=(",", ":")) for record in records]
    write_text(path, "\n".join(lines) + ("\n" if lines else ""))
