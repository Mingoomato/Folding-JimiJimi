from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from codegate_api.documents.store import DocumentStateStore, PendingDocumentEvent

logger = logging.getLogger(__name__)


class DocumentEventPipeline(Protocol):
    async def process_document_event(self, payload: dict[str, Any]) -> str: ...


class DocumentSyncWorker:
    def __init__(
        self,
        *,
        pipeline: DocumentEventPipeline,
        state: DocumentStateStore,
        retry_delays: tuple[float, ...] = (0.1, 0.5, 2.0),
    ) -> None:
        self._pipeline = pipeline
        self._state = state
        self._retry_delays = retry_delays
        self._wake = asyncio.Event()
        self._stopping = False
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        recovered = self._state.recover_interrupted_events()
        if recovered:
            logger.warning("recovered %d interrupted document sync event(s)", recovered)
        self._task = asyncio.create_task(self._run(), name="codegate-document-sync-v2")
        if self._pending_events(limit=1):
            self.notify()

    def notify(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopping = True
        self._wake.set()
        await self._task
        self._task = None

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            if self._stopping:
                return
            for event in self._pending_events():
                if self._stopping:
                    return
                if not self._state.claim_event(event.event_id):
                    continue
                try:
                    graph_version = await self._pipeline.process_document_event(event.payload)
                except Exception as error:
                    code = str(getattr(error, "code", "knowledge_sync_failed"))
                    retryable = bool(getattr(error, "retryable", True))
                    self._state.fail_event(
                        event.event_id,
                        code=code,
                        message=str(error),
                        retryable=retryable,
                    )
                    logger.exception("document sync v2 failed")
                    if retryable and event.attempts < len(self._retry_delays):
                        await asyncio.sleep(self._retry_delays[event.attempts])
                        self._wake.set()
                else:
                    self._state.complete_event(
                        event.event_id,
                        graph_version_after=graph_version,
                    )
            if self._pending_events(limit=1) and not self._stopping:
                self._wake.set()

    def _pending_events(self, *, limit: int = 10) -> list[PendingDocumentEvent]:
        return self._state.pending_events(
            limit=limit,
            max_attempts=len(self._retry_delays),
        )
