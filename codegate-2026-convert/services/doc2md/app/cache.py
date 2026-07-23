import hashlib
from pathlib import Path
from threading import Lock

from pydantic import ValidationError

from app import __version__
from app.data_paths import atomic_write_text, state_path
from app.schemas import RawConversion

_lock = Lock()


def _cache_key(path: Path) -> str:
    # mtime + size is not a content identity: an in-place equal-size rewrite can
    # preserve both and would return stale text under a new source hash. Include
    # the actual bytes so a cache hit always belongs to this source version.
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    # Converter changes can alter OCR output for identical source bytes. Keep
    # stale results from an older runtime from bypassing the repaired pipeline.
    raw = f"{__version__}|{path.resolve()}|{digest.hexdigest()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get(path: Path) -> RawConversion | None:
    key = _cache_key(path)
    cache_file = state_path("raw", f"{key}.json")
    if not cache_file.exists():
        return None
    try:
        return RawConversion.model_validate_json(cache_file.read_text(encoding="utf-8"))
    except (ValidationError, OSError, ValueError):
        # An entry written by an older RawConversion shape, or a half-written
        # file. A stale cache must never be able to fail a conversion — treat it
        # as a miss and let the caller redo the work.
        return None


def set(path: Path, result: RawConversion) -> None:
    key = _cache_key(path)
    with _lock:
        cache_file = state_path("raw", f"{key}.json")
        atomic_write_text(cache_file, result.model_dump_json())
