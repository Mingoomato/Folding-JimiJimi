"""Bearer auth and the source-path allowlist.

Both are opt-in, so these tests set the environment explicitly. The point of the
controls is that ``/convert`` opens an absolute server path: without them, a
publicly reachable doc2md is an arbitrary file read.
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def source(tmp_path):
    p = tmp_path / "문서.md"
    p.write_text("# 제목\n\n## 절\n\n본문.\n", encoding="utf-8")
    return p


def _body(source):
    return {"source": {"kind": "path", "path": str(source)}}


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------


def test_no_token_configured_means_no_auth_required(client, source, monkeypatch):
    """The co-located loopback deployment must keep working untouched."""
    monkeypatch.delenv("DOC2MD_API_TOKEN", raising=False)
    assert client.post("/v2/convert", json=_body(source)).status_code == 200


def test_configured_token_is_required(client, source, monkeypatch):
    monkeypatch.setenv("DOC2MD_API_TOKEN", "s3cret")

    r = client.post("/v2/convert", json=_body(source))
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHORIZED"
    assert r.json()["retryable"] is False


def test_wrong_token_is_rejected(client, source, monkeypatch):
    monkeypatch.setenv("DOC2MD_API_TOKEN", "s3cret")
    r = client.post(
        "/v2/convert", json=_body(source), headers={"Authorization": "Bearer nope"}
    )
    assert r.status_code == 401


def test_correct_token_is_accepted(client, source, monkeypatch):
    monkeypatch.setenv("DOC2MD_API_TOKEN", "s3cret")
    r = client.post(
        "/v2/convert", json=_body(source), headers={"Authorization": "Bearer s3cret"}
    )
    assert r.status_code == 200


# --------------------------------------------------------------------------
# path allowlist
# --------------------------------------------------------------------------


def test_path_inside_an_allowed_root_is_accepted(client, source, monkeypatch):
    monkeypatch.setenv("DOC2MD_ALLOWED_ROOTS", str(source.parent))
    assert client.post("/v2/convert", json=_body(source)).status_code == 200


def test_path_outside_every_allowed_root_is_refused(
    client, source, tmp_path, monkeypatch
):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("DOC2MD_ALLOWED_ROOTS", str(allowed))

    r = client.post("/v2/convert", json=_body(source))
    assert r.status_code == 403
    assert r.json()["code"] == "SOURCE_UNSAFE"


def test_dot_dot_cannot_escape_an_allowed_root(client, tmp_path, monkeypatch):
    """Resolution happens before the check, so traversal is not a way out."""
    secret = tmp_path / "비밀.md"
    secret.write_text("# 비밀\n\n## 절\n\n유출되면 안 되는 내용.\n", encoding="utf-8")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("DOC2MD_ALLOWED_ROOTS", str(allowed))

    r = client.post(
        "/v2/convert",
        json={"source": {"kind": "path", "path": str(allowed / ".." / "비밀.md")}},
    )
    assert r.status_code == 403
    assert "비밀" not in r.text or r.json()["code"] == "SOURCE_UNSAFE"


def test_allowlist_does_not_leak_the_permitted_roots(client, source, tmp_path, monkeypatch):
    allowed = tmp_path / "매우비밀스러운경로"
    allowed.mkdir()
    monkeypatch.setenv("DOC2MD_ALLOWED_ROOTS", str(allowed))

    r = client.post("/v2/convert", json=_body(source))
    assert "매우비밀스러운경로" not in r.text


def test_uploaded_bytes_are_unaffected_by_the_path_allowlist(
    client, source, tmp_path, monkeypatch
):
    """Uploads never touch a caller-named path, so the allowlist does not apply."""
    monkeypatch.setenv("DOC2MD_ALLOWED_ROOTS", str(tmp_path / "somewhere-else"))

    r = client.post(
        "/v2/convert",
        json={
            "source": {
                "kind": "bytes",
                "filename": "업로드.md",
                "content_base64": base64.b64encode(source.read_bytes()).decode(),
            }
        },
    )
    assert r.status_code == 200


def test_upload_filename_cannot_write_outside_the_temp_dir(client, source):
    """A traversal filename must be reduced to its basename."""
    r = client.post(
        "/v2/convert",
        json={
            "source": {
                "kind": "bytes",
                "filename": "../../../evil.md",
                "content_base64": base64.b64encode(source.read_bytes()).decode(),
            }
        },
    )
    assert r.status_code == 200
    assert r.json()["frontmatter"]["source"]["filename"] == "evil.md"


# --------------------------------------------------------------------------
# deep health
# --------------------------------------------------------------------------


def test_deep_health_reports_the_active_posture(client, monkeypatch):
    monkeypatch.setenv("DOC2MD_API_TOKEN", "s3cret")
    monkeypatch.setenv("DOC2MD_ALLOWED_ROOTS", "/srv/source")

    body = client.get("/health?deep=1").json()

    assert body["auth_required"] is True
    assert body["allowlist_enforced"] is True
    assert body["convert"] == "ok", "deep health must actually convert something"


def test_plain_health_stays_cheap(client):
    assert client.get("/health").json()["status"] == "ok"
