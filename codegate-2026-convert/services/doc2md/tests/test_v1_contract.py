"""Guard: ``POST /convert`` must keep the exact v0.1.0 response shape.

The consuming backend (``codegate-2026-agent``,
``src/codegate_api/integrations/doc2md.py``) validates our response with a
pydantic model declared ``extra="forbid"``. Any additional top-level key — even
one serialized as ``null`` — makes that model raise, and the backend rejects the
whole conversion as ``DOC2MD_RESPONSE_INVALID``.

This happened for real: adding ``chunks`` to the response broke the integration
with no error on our side, because we never call the consumer. The models below
are a deliberate copy of the consumer's, so a field added to ``/convert`` fails
here instead of in someone else's service.

Do not "fix" a failure by relaxing these models. Put the new field on
``POST /v2/convert`` instead.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field

from app.main import app

# ---------------------------------------------------------------------------
# Verbatim copy of the consumer's models. Keep in sync with
# codegate-2026-agent/src/codegate_api/integrations/doc2md.py
# ---------------------------------------------------------------------------


class ConsumerSource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    filename: str
    uri: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ConsumerFrontmatter(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: str
    id: str
    title: str
    doc_type: str
    language: str
    revision: str
    status: str
    official_number: str | None = None
    authority_level: str | None = None
    issuing_org: str | None = None
    issued_on: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    source: ConsumerSource
    access: str
    tags: list[str]
    aliases: list[str]


class ConsumerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markdown: str
    frontmatter: ConsumerFrontmatter
    body: str
    format: str
    library_used: str
    warnings: list[str]
    cached: bool


V1_KEYS = {
    "markdown",
    "frontmatter",
    "body",
    "format",
    "library_used",
    "warnings",
    "cached",
}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def source(tmp_path):
    p = tmp_path / "계약테스트.md"
    p.write_text(
        "# 계약 테스트 문서\n\n## 개요\n\n본문 한 줄.\n", encoding="utf-8"
    )
    return p


def _convert(client: TestClient, source, **body) -> dict:
    response = client.post("/convert", json={"path": str(source), **body})
    assert response.status_code == 200, response.text
    return response.json()


def test_response_has_exactly_the_seven_v1_keys(client, source):
    assert set(_convert(client, source)) == V1_KEYS


def test_response_validates_against_the_consumers_model(client, source):
    ConsumerResponse.model_validate(_convert(client, source))


def test_chunking_does_not_leak_chunks_into_the_v1_response(client, source):
    """The regression that actually broke the integration."""
    payload = _convert(client, source, chunking={"enabled": True})
    assert "chunks" not in payload
    ConsumerResponse.model_validate(payload)


def test_metadata_overrides_are_preserved(client, source):
    """The consumer rejects the response unless its overrides come back intact."""
    overrides = {
        "id": "REG-000001",
        "doc_type": "regulation",
        "revision": "3",
        "status": "active",
        "authority_level": "internal-standard",
        "uri": "source://계약테스트.md",
        "access": "internal",
    }
    fm = _convert(client, source, metadata=overrides)["frontmatter"]

    assert fm["id"] == overrides["id"]
    assert fm["doc_type"] == overrides["doc_type"]
    assert fm["revision"] == overrides["revision"]
    assert fm["status"] == overrides["status"]
    assert fm["authority_level"] == overrides["authority_level"]
    assert fm["source"]["uri"] == overrides["uri"]
    assert fm["access"] == overrides["access"]


def test_body_is_contained_in_markdown(client, source):
    """The consumer asserts ``body in markdown`` before accepting the result."""
    payload = _convert(client, source)
    assert payload["body"].strip()
    assert payload["body"] in payload["markdown"]


def test_missing_source_is_404(client, tmp_path):
    """The consumer maps 404 to DOC2MD_SOURCE_NOT_FOUND and anything else to
    DOC2MD_CONVERSION_FAILED, so the status code is part of the contract."""
    response = client.post("/convert", json={"path": str(tmp_path / "없음.pdf")})
    assert response.status_code == 404
