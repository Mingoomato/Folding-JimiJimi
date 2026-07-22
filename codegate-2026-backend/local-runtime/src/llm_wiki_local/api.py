from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

from llm_wiki_local.builder_gateway import WikiBuilderGateway
from llm_wiki_local.config import RuntimeSettings
from llm_wiki_local.converter import Doc2MdClient
from llm_wiki_local.errors import EventConflictError, SourceAccessError
from llm_wiki_local.input_snapshots import InputSnapshots
from llm_wiki_local.models import EventSubmission, SourceEvent
from llm_wiki_local.runtime import LocalWikiRuntime
from llm_wiki_local.store import StateStore


def build_runtime(settings: RuntimeSettings) -> LocalWikiRuntime:
    store = StateStore(settings.database_path)
    converter = Doc2MdClient(
        settings.converter_url,
        timeout_seconds=settings.converter_timeout_seconds,
        poll_seconds=settings.converter_poll_seconds,
    )
    snapshots = InputSnapshots(settings.input_snapshots_dir, settings.bootstrap_input_dir)
    builder = WikiBuilderGateway(
        config_path=settings.config_path,
        storage_root=settings.storage_root,
        tenant_id=settings.tenant_id,
        wiki_id=settings.wiki_id,
        actor_id=settings.actor_id,
        synthetic_corpus=settings.synthetic_corpus,
        run_enrichment=settings.run_enrichment,
    )
    return LocalWikiRuntime(
        settings,
        store=store,
        converter=converter,
        snapshots=snapshots,
        builder=builder,
    )


def create_app(
    settings: RuntimeSettings | None = None,
    runtime: LocalWikiRuntime | None = None,
) -> FastAPI:
    selected_settings = settings or RuntimeSettings.from_env()
    selected_runtime = runtime or build_runtime(selected_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        selected_runtime.start()
        try:
            yield
        finally:
            selected_runtime.shutdown()

    app = FastAPI(title="LLM Wiki Local Runtime", version="0.1.0", lifespan=lifespan)
    if selected_settings.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(selected_settings.allowed_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "X-LLMWiki-Token"],
        )
    app.state.runtime = selected_runtime
    app.state.settings = selected_settings

    def require_token(
        authorization: str | None = Header(default=None),
        x_llmwiki_token: str | None = Header(default=None),
    ) -> None:
        expected = selected_settings.api_token
        if expected is None:
            return
        supplied = x_llmwiki_token
        if authorization and authorization.lower().startswith("bearer "):
            supplied = authorization[7:]
        if supplied is None or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "converter_url": selected_settings.converter_url,
            "tenant_id": selected_settings.tenant_id,
            "wiki_id": selected_settings.wiki_id,
        }

    @app.post(
        "/api/v1/source-events",
        response_model=EventSubmission,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_token)],
    )
    def submit_event(event: SourceEvent) -> EventSubmission:
        try:
            return selected_runtime.submit_event(event)
        except SourceAccessError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except EventConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.get("/api/v1/jobs/{job_id}", dependencies=[Depends(require_token)])
    def get_job(job_id: str) -> dict[str, Any]:
        job = selected_runtime.store.job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown job")
        return job

    @app.get("/api/v1/sources", dependencies=[Depends(require_token)])
    def list_sources() -> dict[str, Any]:
        return {"sources": selected_runtime.store.list_sources()}

    return app


def main() -> None:
    settings = RuntimeSettings.from_env()
    uvicorn.run(
        create_app(settings),
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
