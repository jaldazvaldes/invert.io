"""Conservative request spacing shared by local public and private clients."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable


class RequestGate:
    """At most one request start per second, including shared server backoff.

    A single event loop owns this gate. ``defer`` also affects callers already
    sleeping in ``wait``; a retry never bypasses the same shared gate.
    """

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._monotonic = monotonic
        self._sleep = sleep
        self._next_request_at = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            while (delay := self._next_request_at - self._monotonic()) > 0:
                await self._sleep(delay)
            self._next_request_at = self._monotonic() + 1.0

    def defer(self, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("The request delay must be finite and non-negative")
        self._next_request_at = max(self._next_request_at, self._monotonic() + seconds)
