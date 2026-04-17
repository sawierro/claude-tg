import asyncio
import logging
import time
from collections import defaultdict, deque

logger = logging.getLogger(__name__)


class RateLimiter:
    """Sliding-window rate limiter per key (e.g. chat_id).

    Non-blocking: each call either returns immediately with True/False. Thread-
    safe for asyncio (no actual threads — single event loop).
    """

    def __init__(self, max_per_minute: int):
        if max_per_minute <= 0:
            raise ValueError("max_per_minute must be > 0")
        self._max = max_per_minute
        self._window = 60.0
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def check(self, key: int) -> bool:
        """Return True if the call is allowed, False if rate-limited."""
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] >= self._window:
            hits.popleft()
        if len(hits) >= self._max:
            return False
        hits.append(now)
        return True

    def reset(self, key: int) -> None:
        self._hits.pop(key, None)


class ConcurrencyGuard:
    """Cap on concurrent in-flight provider calls for the whole bot."""

    def __init__(self, limit: int):
        if limit <= 0:
            raise ValueError("limit must be > 0")
        self._sem = asyncio.Semaphore(limit)
        self._limit = limit

    async def __aenter__(self):
        await self._sem.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._sem.release()

    @property
    def limit(self) -> int:
        return self._limit
