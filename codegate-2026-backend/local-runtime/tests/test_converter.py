from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from llm_wiki_local.converter import Doc2MdClient
from llm_wiki_local.errors import ConversionError


def test_async_conversion_is_polled_until_result(tmp_path: Path) -> None:
    source = tmp_path / "document.pdf"
    source.write_bytes(b"pdf")
    polls = 0
    submitted: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        if request.method == "POST" and request.url.path == "/convert/async":
            submitted.update(json.loads(request.content))
            return httpx.Response(202, json={"job_id": "remote-job", "status": "queued"})
        if request.url.path == "/jobs/remote-job":
            polls += 1
            status = "done" if polls > 1 else "running"
            return httpx.Response(
                200,
                json={"status": status, "phase": "convert", "message": status},
            )
        if request.url.path == "/jobs/remote-job/result":
            return httpx.Response(
                200,
                json={"body": "# 제목\n\n본문", "warnings": [], "frontmatter": {}},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = Doc2MdClient(
        "http://converter.local",
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
    )
    result = client.convert(source, {"id": "REG-000001"}, job_id="local-job")

    assert result["body"].startswith("# 제목")
    assert submitted["job_id"] == "local-job"
    assert submitted["metadata"]["id"] == "REG-000001"
    assert submitted["chunking"]["enabled"] is True
    assert polls == 2


def test_blocking_conversion_warning_is_rejected() -> None:
    with pytest.raises(ConversionError, match="blocking warning"):
        Doc2MdClient._validate_result(
            {
                "body": "임시 본문",
                "warnings": ["scanned_pdf_ocr_failed: no text could be recovered"],
            }
        )
