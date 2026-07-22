from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Protocol

from codegate_api.state.store import StateStore

logger = logging.getLogger(__name__)


class KnowledgePipeline(Protocol):
    async def process_pending(self) -> str: ...


class KnowledgeSyncWorker:
    """Lifespan-owned outbox worker for conversion, indexes, and atomic publication."""

    def __init__(
        self,
        *,
        pipeline: KnowledgePipeline,
        state_store: StateStore,
        retry_delays: tuple[float, ...] = (0.05, 0.2, 1.0),
    ) -> None:
        self._pipeline = pipeline
        self._state = state_store
        self._retry_delays = retry_delays or (1.0,)
        self._wake = asyncio.Event()
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._last_error: Exception | None = None
        self._last_error_at: datetime | None = None
        self._last_progress_at: datetime | None = None

    def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="codegate-knowledge-sync")
        if self._state.pending_events():
            self.notify()

    def notify(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stopping = True
        self._wake.set()
        await task
        self._task = None

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            if self._stopping:
                return
            for retry_delay in self._retry_delays:
                try:
                    pending_before = self._state.pending_events()
                    if not pending_before:
                        self._last_error = None
                        break
                    await self._pipeline.process_pending()
                    self._last_progress_at = datetime.now(UTC)
                    self._last_error = None
                except Exception as error:
                    self._last_error = error
                    self._last_error_at = datetime.now(UTC)
                    logger.exception("knowledge sync worker iteration failed")
                else:
                    try:
                        if not self._state.pending_events():
                            break
                    except Exception as error:
                        self._last_error = error
                        self._last_error_at = datetime.now(UTC)
                        logger.exception("knowledge sync worker state check failed")
                if self._stopping:
                    return
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=retry_delay)
                except TimeoutError:
                    pass
                else:
                    self._wake.clear()
                    if self._stopping:
                        return
            else:
                # Re-enter bounded backoff instead of stranding pending work.
                self._wake.set()

    @property
    def healthy(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def last_error(self) -> Exception | None:
        return self._last_error

    @property
    def last_error_at(self) -> datetime | None:
        return self._last_error_at

    @property
    def last_progress_at(self) -> datetime | None:
        return self._last_progress_at
