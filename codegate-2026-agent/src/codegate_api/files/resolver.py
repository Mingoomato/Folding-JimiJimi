from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse


class SourceResolutionError(ValueError):
    """Raised when a source URI cannot be resolved inside the allowed root."""


class SourceUriResolver:
    def __init__(self, source_root: Path) -> None:
        self._source_root = source_root.resolve()

    @property
    def source_root(self) -> Path:
        return self._source_root

    def relative_path(self, source_uri: str) -> PurePosixPath:
        parsed = urlparse(source_uri)
        if parsed.scheme != "source":
            raise SourceResolutionError("only source:// URIs are allowed")
        if parsed.params or parsed.query or parsed.fragment:
            raise SourceResolutionError("source URI must not contain params, query, or fragment")

        relative = unquote(f"{parsed.netloc}{parsed.path}").lstrip("/")
        if not relative:
            raise SourceResolutionError("source URI must include a relative path")
        relative_path = PurePosixPath(relative)
        if relative_path.is_absolute() or any(
            part in {"", ".", ".."} for part in relative_path.parts
        ):
            raise SourceResolutionError("source URI contains an unsafe path component")
        return relative_path

    def resolve(self, source_uri: str) -> Path:
        relative = self.relative_path(source_uri)
        candidate = (self._source_root / Path(*relative.parts)).resolve()
        if not candidate.is_relative_to(self._source_root):
            raise SourceResolutionError("source URI escapes the allowed root")
        return candidate
