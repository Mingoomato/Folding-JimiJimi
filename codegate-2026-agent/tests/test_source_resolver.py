from pathlib import Path

import pytest

from codegate_api.files.resolver import SourceResolutionError, SourceUriResolver


def test_source_resolver_keeps_paths_inside_root(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    resolver = SourceUriResolver(source_root)

    assert (
        resolver.resolve("source://regulations/example.md")
        == (source_root / "regulations/example.md").resolve()
    )


@pytest.mark.parametrize(
    "uri",
    [
        "file:///etc/passwd",
        "source://../outside.md",
        "source://regulations/%2E%2E/%2E%2E/outside.md",
    ],
)
def test_source_resolver_rejects_invalid_or_escaping_paths(tmp_path: Path, uri: str) -> None:
    resolver = SourceUriResolver(tmp_path / "source")

    with pytest.raises(SourceResolutionError):
        resolver.resolve(uri)
