from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx

from llm_wiki_local.errors import ConversionError

ProgressCallback = Callable[[str, str], None]


class Doc2MdClient:
    """Client for the local codegate-2026-convert FastAPI service."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 1800.0,
        poll_seconds: float = 0.5,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        self.transport = transport
        self.sleeper = sleeper

    def health(self) -> bool:
        try:
            with self._client() as client:
                response = client.get("/health")
                response.raise_for_status()
                return response.json().get("status") == "ok"
        except (httpx.HTTPError, ValueError):
            return False

    def convert(
        self,
        path: Path,
        metadata: dict[str, Any],
        *,
        job_id: str,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        payload = {
            "path": str(path.resolve()),
            "metadata": metadata,
            "chunking": {
                "enabled": True,
                "strategy": "fixed",
                "max_chars": 12000,
                "min_chars": 2400,
                "split_level": 2,
            },
            "job_id": job_id,
        }
        deadline = time.monotonic() + self.timeout_seconds
        with self._client() as client:
            response = self._request(client, "POST", "/convert/async", json=payload)
            remote_job_id = str(response.get("job_id") or job_id)
            while True:
                if time.monotonic() >= deadline:
                    raise ConversionError(
                        f"doc2md conversion timed out after {self.timeout_seconds:g}s"
                    )
                snapshot = self._request(client, "GET", f"/jobs/{remote_job_id}")
                status = str(snapshot.get("status", "unknown"))
                phase = str(snapshot.get("phase", status))
                message = str(snapshot.get("message", ""))
                if progress is not None:
                    progress(phase, message)
                if status == "done":
                    break
                if status == "failed":
                    raise ConversionError(
                        str(snapshot.get("error") or "doc2md conversion failed")
                    )
                self.sleeper(self.poll_seconds)
            result = self._request(client, "GET", f"/jobs/{remote_job_id}/result")
        self._validate_result(result)
        return result

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            timeout=min(self.timeout_seconds, 600.0),
            transport=self.transport,
        )

    @staticmethod
    def _request(
        client: httpx.Client,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            value = response.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text
            with suppress(ValueError, AttributeError):
                detail = str(exc.response.json().get("detail", detail))
            raise ConversionError(f"doc2md {method} {path} failed: {detail}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ConversionError(f"doc2md {method} {path} failed: {exc}") from exc
        if not isinstance(value, dict):
            raise ConversionError(f"doc2md {method} {path} returned a non-object response")
        return value

    @staticmethod
    def _validate_result(result: dict[str, Any]) -> None:
        body = result.get("body")
        if not isinstance(body, str) or not body.strip():
            raise ConversionError("doc2md returned an empty body")
        warnings = result.get("warnings", [])
        if not isinstance(warnings, list):
            raise ConversionError("doc2md returned invalid warnings")
        blocking = (
            "empty_output",
            "scanned_pdf_ocr_failed",
            "pptx_ocr_skipped",
        )
        blocked = [str(item) for item in warnings if str(item).startswith(blocking)]
        if blocked:
            raise ConversionError("doc2md blocking warning: " + "; ".join(blocked))
