from __future__ import annotations

import asyncio

import httpx

from llm_wiki_local.api import create_app


def test_event_endpoint_requires_local_token(runtime, settings) -> None:
    protected = settings.__class__(**{**settings.__dict__, "api_token": "secret-token"})
    source = settings.allowed_source_roots[0] / "api.txt"
    source.write_text("API 본문", encoding="utf-8")
    app = create_app(protected, runtime)
    payload = {
        "event_id": "api-event",
        "event_type": "created",
        "source_id": "api-source",
        "sequence": 1,
        "relative_path": "api.txt",
        "source_path": str(source),
        "metadata": {"id": "GEN-000001", "title": "API 문서"},
    }

    async def request() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=transport,
                base_url="http://local-runtime",
            ) as client,
        ):
            unauthorized = await client.post("/api/v1/source-events", json=payload)
            accepted = await client.post(
                "/api/v1/source-events",
                json=payload,
                headers={"Authorization": "Bearer secret-token"},
            )
        return unauthorized, accepted

    unauthorized, accepted = asyncio.run(request())

    assert unauthorized.status_code == 401
    assert accepted.status_code == 202
    assert accepted.json()["event_id"] == "api-event"
