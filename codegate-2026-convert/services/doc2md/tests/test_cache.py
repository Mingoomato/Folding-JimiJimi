from __future__ import annotations

import os
from pathlib import Path

from app import cache
from app.schemas import RawConversion


def _result(body: str) -> RawConversion:
    return RawConversion(body=body, format="md", library_used="test")


def test_cache_identity_uses_content_not_only_mtime_and_size(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("DOC2MD_DATA_ROOT", str(tmp_path / "state"))
    source = tmp_path / "source.md"
    source.write_text("old body", encoding="utf-8")
    original_stat = source.stat()
    cache.set(source, _result("converted old body"))

    source.write_text("new body", encoding="utf-8")
    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    assert source.stat().st_size == len("old body")
    assert cache.get(source) is None


def test_cache_state_uses_configured_data_root(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path / "runtime"
    monkeypatch.setenv("DOC2MD_DATA_ROOT", str(data_root))
    source = tmp_path / "source.md"
    source.write_text("content", encoding="utf-8")

    cache.set(source, _result("converted"))

    assert list((data_root / "raw").glob("*.json"))
