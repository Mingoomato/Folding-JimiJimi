from __future__ import annotations

import base64
import hashlib
import io
import json
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codegate_api.files.resolver import SourceUriResolver
from codegate_api.integrations.doc2md import (
    ConversionResult,
    ConversionSection,
    Doc2MdClient,
    Doc2MdError,
)
from codegate_api.knowledge.access import AccessContext
from codegate_api.knowledge.pipeline import _restore_section_anchors
from codegate_api.knowledge.schemas import ManifestEntry


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")


def _manifest() -> ManifestEntry:
    row = json.loads(
        Path("demo/llm-wiki/manifest.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    return ManifestEntry.model_validate(row)


def _response(manifest: ManifestEntry, sha256: str) -> dict[str, Any]:
    body = "# 문서\n\n## 본문\n\n내용\n"
    h2_start = body.index("## 본문")
    canonical_sha256 = hashlib.sha256(body.encode()).hexdigest()
    return {
        "markdown": f"---\nid: REG-000001\n---\n{body}",
        "frontmatter": {
            "schema_version": "1.1.0",
            "id": manifest.id,
            "title": manifest.title,
            "doc_type": manifest.doc_type,
            "language": manifest.language,
            "revision": manifest.revision,
            "status": manifest.status,
            "official_number": manifest.official_number,
            "authority_level": manifest.authority_level,
            "issuing_org": manifest.issuing_org,
            "issued_on": manifest.issued_on.isoformat() if manifest.issued_on else None,
            "effective_from": (
                manifest.effective_from.isoformat() if manifest.effective_from else None
            ),
            "effective_to": manifest.effective_to.isoformat() if manifest.effective_to else None,
            "source": {
                "filename": Path(manifest.source.filename).name,
                "uri": manifest.source.uri,
                "sha256": sha256,
            },
            "canonical_sha256": canonical_sha256,
            "converter_version": "0.2.0",
            "access": manifest.access.value,
            "tags": manifest.tags,
            "aliases": manifest.aliases,
        },
        "body": body,
        "format": "md",
        "library_used": "markitdown",
        "warnings": [],
        "cached": False,
        "source_sha256": sha256,
        "canonical_sha256": canonical_sha256,
        "converter": {
            "name": "doc2md",
            "version": "0.2.0",
            "library": "markitdown",
            "ocr_device": "unavailable",
        },
        "sections": [
            {
                "ordinal": 1,
                "anchor_hint": "sec-001",
                "stable_key": "h-11111111",
                "level": 1,
                "heading": "문서",
                "heading_path": ["문서"],
                "char_start": 0,
                "char_end": h2_start,
                "source_page": None,
            },
            {
                "ordinal": 2,
                "anchor_hint": "sec-002",
                "stable_key": "h-22222222",
                "level": 2,
                "heading": "본문",
                "heading_path": ["문서", "본문"],
                "char_start": h2_start,
                "char_end": len(body),
                "source_page": None,
            },
        ],
        "diagnostics": [],
        "budget": {
            "est_tokens": 10,
            "budget_tokens": 1000,
            "routing": "processed",
            "reason": None,
        },
        "chunks": None,
    }


def test_doc2md_adapter_forces_stable_metadata_and_validates_hash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    source_root = tmp_path / "source"
    source = source_root / "regulations/REG-000001.md"
    source.parent.mkdir(parents=True)
    source.write_text("source", encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        captured.update(json.loads(request.data))
        assert timeout == 120.0
        assert request.full_url == "http://doc2md.internal/v2/convert"
        assert request.get_header("Authorization") == "Bearer test-doc2md-token"
        return _FakeResponse(_response(manifest, manifest.source.sha256))

    monkeypatch.setattr("codegate_api.integrations.doc2md.urlopen", fake_urlopen)
    client = Doc2MdClient(
        base_url="http://doc2md.internal",
        resolver=SourceUriResolver(source_root),
        timeout_seconds=120.0,
        api_token="test-doc2md-token",
    )

    converted = client.convert(manifest, expected_source_sha256=manifest.source.sha256)

    assert captured["source"] == {"kind": "path", "path": str(source.resolve())}
    assert captured["expected_source_sha256"] == manifest.source.sha256
    assert captured["chunking"] == {"enabled": False}
    assert captured["metadata"]["id"] == "REG-000001"
    assert captured["metadata"]["uri"] == "source://regulations/REG-000001.md"
    assert captured["metadata"]["revision"] == manifest.revision
    assert captured["metadata"]["authority_level"] == manifest.authority_level
    assert captured["metadata"]["issued_on"] == "2026-06-01"
    assert captured["metadata"]["effective_from"] == "2026-07-01"
    assert converted.converter == "doc2md:0.2.0:markitdown"
    assert converted.converter_version == "0.2.0"
    assert [section.stable_key for section in converted.sections] == [
        "h-11111111",
        "h-22222222",
    ]


def test_doc2md_bytes_transport_hashes_and_bounds_the_approved_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    source_root = tmp_path / "source"
    source = source_root / "regulations/REG-000001.md"
    source.parent.mkdir(parents=True)
    source_bytes = b"approved source"
    source.write_bytes(source_bytes)
    expected_sha256 = hashlib.sha256(source_bytes).hexdigest()
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        del timeout
        captured.update(json.loads(request.data))
        return _FakeResponse(_response(manifest, expected_sha256))

    monkeypatch.setattr("codegate_api.integrations.doc2md.urlopen", fake_urlopen)
    client = Doc2MdClient(
        base_url="http://doc2md.internal",
        resolver=SourceUriResolver(source_root),
        timeout_seconds=120.0,
        source_kind="bytes",
        max_source_bytes=len(source_bytes),
    )

    converted = client.convert(manifest, expected_source_sha256=expected_sha256)

    assert captured["source"]["kind"] == "bytes"
    assert captured["source"]["filename"] == "REG-000001.md"
    assert base64.b64decode(captured["source"]["content_base64"]) == source_bytes
    assert converted.source_sha256 == expected_sha256


def test_doc2md_bytes_transport_rejects_stale_or_oversized_source_before_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    source_root = tmp_path / "source"
    source = source_root / "regulations/REG-000001.md"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    monkeypatch.setattr(
        "codegate_api.integrations.doc2md.urlopen",
        lambda *_args, **_kwargs: pytest.fail("network must not be called"),
    )
    client = Doc2MdClient(
        base_url="http://doc2md.internal",
        resolver=SourceUriResolver(source_root),
        timeout_seconds=120.0,
        source_kind="bytes",
        max_source_bytes=6,
    )

    with pytest.raises(Doc2MdError) as stale:
        client.convert(manifest, expected_source_sha256="0" * 64)
    assert stale.value.code == "DOC2MD_STALE_SOURCE"

    source.write_bytes(b"source!")
    with pytest.raises(Doc2MdError) as oversized:
        client.convert(
            manifest,
            expected_source_sha256=hashlib.sha256(b"source!").hexdigest(),
        )
    assert oversized.value.code == "DOC2MD_SOURCE_TOO_LARGE"


def test_doc2md_network_failure_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    source_root = tmp_path / "source"
    source = source_root / "regulations/REG-000001.md"
    source.parent.mkdir(parents=True)
    source.write_text("source", encoding="utf-8")
    monkeypatch.setattr(
        "codegate_api.integrations.doc2md.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("timed out")),
    )
    client = Doc2MdClient(
        base_url="http://doc2md.internal",
        resolver=SourceUriResolver(source_root),
        timeout_seconds=120.0,
    )

    with pytest.raises(Doc2MdError) as raised:
        client.convert(manifest, expected_source_sha256=manifest.source.sha256)

    assert raised.value.code == "DOC2MD_UNAVAILABLE"
    assert raised.value.retryable is True
    assert "timed out" not in str(raised.value)


def test_restore_section_anchors_preserves_id_when_heading_is_renamed() -> None:
    body = "# 문서\n\n## 변경된 제목\n\n내용\n"
    heading_start = body.index("## 변경된 제목")

    restored = _restore_section_anchors(
        body,
        document_id="REG-000001",
        chunks=[
            {
                "document_id": "REG-000001",
                "heading_path": ["문서", "기존 제목"],
                "section_id": "sec-004",
            }
        ],
        converted_sections=(
            ConversionSection(
                ordinal=1,
                stable_key="h-11111111",
                heading_path=("문서",),
                char_start=0,
                char_end=heading_start,
                source_page=None,
            ),
            ConversionSection(
                ordinal=2,
                stable_key="h-22222222",
                heading_path=("문서", "변경된 제목"),
                char_start=heading_start,
                char_end=len(body),
                source_page=None,
            ),
        ),
    )

    assert '<a id="sec-004"></a>\n## 변경된 제목' in restored


def test_doc2md_adapter_rejects_stale_cache_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    source_root = tmp_path / "source"
    source = source_root / "regulations/REG-000001.md"
    source.parent.mkdir(parents=True)
    source.write_text("source", encoding="utf-8")
    monkeypatch.setattr(
        "codegate_api.integrations.doc2md.urlopen",
        lambda *_args, **_kwargs: _FakeResponse(_response(manifest, "f" * 64)),
    )
    client = Doc2MdClient(
        base_url="http://doc2md.internal",
        resolver=SourceUriResolver(source_root),
        timeout_seconds=120.0,
    )

    with pytest.raises(Doc2MdError) as raised:
        client.convert(manifest, expected_source_sha256=manifest.source.sha256)

    assert raised.value.code == "DOC2MD_STALE_CACHE"


def test_doc2md_adapter_rejects_canonical_hash_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    source_root = tmp_path / "source"
    source = source_root / "regulations/REG-000001.md"
    source.parent.mkdir(parents=True)
    source.write_text("source", encoding="utf-8")
    payload = _response(manifest, manifest.source.sha256)
    payload["canonical_sha256"] = "f" * 64
    monkeypatch.setattr(
        "codegate_api.integrations.doc2md.urlopen",
        lambda *_args, **_kwargs: _FakeResponse(payload),
    )
    client = Doc2MdClient(
        base_url="http://doc2md.internal",
        resolver=SourceUriResolver(source_root),
        timeout_seconds=120.0,
    )

    with pytest.raises(Doc2MdError) as raised:
        client.convert(manifest, expected_source_sha256=manifest.source.sha256)

    assert raised.value.code == "DOC2MD_CANONICAL_HASH_MISMATCH"


def test_doc2md_adapter_preserves_structured_retryability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    source_root = tmp_path / "source"
    source = source_root / "regulations/REG-000001.md"
    source.parent.mkdir(parents=True)
    source.write_text("source", encoding="utf-8")
    error_body = json.dumps(
        {
            "code": "OCR_ENGINE_UNAVAILABLE",
            "message": "internal detail",
            "retryable": True,
            "detail": "/private/path",
        }
    ).encode()
    remote_error = HTTPError(
        "http://doc2md.internal/v2/convert",
        503,
        "unavailable",
        hdrs=None,
        fp=io.BytesIO(error_body),
    )
    monkeypatch.setattr(
        "codegate_api.integrations.doc2md.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(remote_error),
    )
    client = Doc2MdClient(
        base_url="http://doc2md.internal",
        resolver=SourceUriResolver(source_root),
        timeout_seconds=120.0,
    )

    with pytest.raises(Doc2MdError) as raised:
        client.convert(manifest, expected_source_sha256=manifest.source.sha256)

    assert raised.value.code == "DOC2MD_OCR_UNAVAILABLE"
    assert raised.value.retryable is True
    assert "/private/path" not in str(raised.value)


def test_sync_pipeline_invokes_configured_doc2md_adapter(
    client: TestClient,
    auth_headers: dict[str, str],
    test_app: FastAPI,
) -> None:
    source_root = test_app.state.container.settings.resolved_source_root()

    class FakeDoc2Md:
        def __init__(self) -> None:
            self.expected_hashes: list[str] = []

        def convert(
            self,
            manifest: ManifestEntry,
            *,
            expected_source_sha256: str,
        ) -> ConversionResult:
            self.expected_hashes.append(expected_source_sha256)
            assert manifest.revision == "2"
            source_text = (source_root / manifest.source.filename).read_text(encoding="utf-8")
            assert "3년" in source_text
            text = (
                Path("demo/llm-wiki/docs/regulations/REG-000001.md")
                .read_text(encoding="utf-8")
                .replace("1년", "3년")
            )
            body = text.replace('<a id="sec-004"></a>\n', "")
            h2_start = body.index("## 제4조 보관 기간")
            return ConversionResult(
                canonical_markdown=body,
                body=body,
                format="md",
                converter="fake-doc2md",
                warnings=(),
                cached=False,
                converter_version="0.2.0",
                sections=(
                    ConversionSection(
                        ordinal=1,
                        stable_key="h-11111111",
                        heading_path=("개인정보 처리 규정",),
                        char_start=0,
                        char_end=h2_start,
                        source_page=None,
                    ),
                    ConversionSection(
                        ordinal=2,
                        stable_key="h-22222222",
                        heading_path=("개인정보 처리 규정", "제4조 보관 기간"),
                        char_start=h2_start,
                        char_end=len(body),
                        source_page=None,
                    ),
                ),
            )

    converter = FakeDoc2Md()
    test_app.state.container.pipeline._doc2md = converter  # type: ignore[assignment]  # noqa: SLF001
    preview = client.post(
        "/api/v1/chat/messages",
        headers=auth_headers,
        json={
            "conversation_id": "doc2md-pipeline",
            "message": 'REG-000001에서 "1년"을 "3년"으로 변경해줘',
            "selected_document_id": "REG-000001",
        },
    ).json()["change_plan"]
    approved = client.post(
        f"/api/v1/change-plans/{preview['change_plan_id']}/approve",
        headers={**auth_headers, "Idempotency-Key": "doc2md-pipeline-key"},
        json={"plan_hash": preview["plan_hash"]},
    )
    assert approved.status_code == 202

    execution_id = approved.json()["execution_id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        execution = client.get(f"/api/v1/executions/{execution_id}", headers=auth_headers).json()
        if execution["sync_status"] == "published":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("doc2md-backed sync did not publish")

    assert converter.expected_hashes == [approved.json()["after_sha256"]]
    manifest = test_app.state.container.catalog.snapshot().get_manifest(
        "REG-000001",
        access_context=AccessContext.anonymous(),
    )
    assert manifest is not None
    assert manifest.conversion.converter == "fake-doc2md"
    assert manifest.conversion.conversion_version == "0.2.0"


def test_sync_pipeline_preserves_non_retryable_doc2md_error(
    client: TestClient,
    auth_headers: dict[str, str],
    test_app: FastAPI,
) -> None:
    class UnauthorizedDoc2Md:
        def convert(self, *_args: object, **_kwargs: object) -> ConversionResult:
            raise Doc2MdError(
                "DOC2MD_UNAUTHORIZED",
                "doc2md 인증에 실패했습니다.",
                retryable=False,
            )

    test_app.state.container.pipeline._doc2md = UnauthorizedDoc2Md()  # type: ignore[assignment]  # noqa: SLF001
    preview = client.post(
        "/api/v1/chat/messages",
        headers=auth_headers,
        json={
            "conversation_id": "doc2md-non-retryable",
            "message": 'REG-000001에서 "1년"을 "3년"으로 변경해줘',
            "selected_document_id": "REG-000001",
        },
    ).json()["change_plan"]
    approved = client.post(
        f"/api/v1/change-plans/{preview['change_plan_id']}/approve",
        headers={**auth_headers, "Idempotency-Key": "doc2md-non-retryable-key"},
        json={"plan_hash": preview["plan_hash"]},
    )
    execution_id = approved.json()["execution_id"]

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        execution = client.get(f"/api/v1/executions/{execution_id}", headers=auth_headers).json()
        if execution["sync_status"] == "failed":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("doc2md failure was not recorded")

    assert execution["error"] == {
        "code": "DOC2MD_UNAUTHORIZED",
        "message": "doc2md 인증에 실패했습니다.",
        "retryable": False,
    }
    assert execution["stage"] == "sync_failed"
    retry = client.post(
        f"/api/v1/executions/{execution_id}/retry-sync",
        headers={**auth_headers, "Idempotency-Key": "doc2md-retry-denied-key"},
    )
    assert retry.status_code == 409
