from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass


@dataclass(slots=True)
class _Window:
    started_at: float
    requests: int


class FixedWindowRateLimiter:
    def __init__(
        self,
        *,
        limit: int,
        window_seconds: int,
        max_identities: int = 10_000,
    ) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._max_identities = max_identities
        self._windows: dict[str, _Window] = {}
        self._lock = asyncio.Lock()

    @property
    def retry_after_seconds(self) -> int:
        return self._window_seconds

    async def allow(self, identity: str) -> bool:
        key = hashlib.sha256(f"codegate-rate-limit-v1\x00{identity}".encode()).hexdigest()
        now = time.monotonic()
        async with self._lock:
            window = self._windows.get(key)
            if window is None or now - window.started_at >= self._window_seconds:
                self._windows[key] = _Window(started_at=now, requests=1)
                self._prune(now)
                return True
            if window.requests >= self._limit:
                return False
            window.requests += 1
            return True

    def _prune(self, now: float) -> None:
        if len(self._windows) < self._max_identities:
            return
        expired = [
            key
            for key, window in self._windows.items()
            if now - window.started_at >= self._window_seconds
        ]
        for key in expired:
            self._windows.pop(key, None)
        while len(self._windows) > self._max_identities:
            self._windows.pop(next(iter(self._windows)))
