from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
import pytest
from ccxt.base.errors import RateLimitExceeded
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from invertio.analysis.market import PublicAnalysisFeed
from invertio.data.throttle import RequestGate
from invertio.manual.client import RevolutXClient, RevolutXClientError


class FakeTime:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.value += seconds
        await asyncio.sleep(0)


def private_client(
    transport: httpx.AsyncClient, gate: RequestGate, clock: FakeTime
) -> RevolutXClient:
    key = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return RevolutXClient(
        "k" * 64,
        key,
        client=transport,
        request_gate=gate,
        timestamp_ms=lambda: 1_700_000_000_000 + int(clock.value * 1000),
    )


class PublicExchange:
    def __init__(self, clock: FakeTime, starts: list[tuple[str, float]]) -> None:
        self.clock, self.starts = clock, starts
        self.failures = 0
        self.last_response_headers: dict[str, str] = {}

    async def fetch(self, path: str) -> dict[str, Any]:
        self.starts.append((path, self.clock.value))
        if self.failures:
            self.failures -= 1
            self.last_response_headers = {"Retry-After": "2500"}
            raise RateLimitExceeded("429")
        return {}

    async def load_markets(self, *, reload: bool = False) -> dict[str, dict[str, Any]]:
        assert reload
        await asyncio.gather(self.fetch("currencies"), self.fetch("markets"))
        return {}

    async def fetch_tickers(self) -> dict[str, dict[str, Any]]:
        await self.fetch("tickers")
        return {}

    async def close(self) -> None:
        pass


async def test_concurrent_request_starts_are_spaced_one_second() -> None:
    clock = FakeTime()
    gate = RequestGate(monotonic=clock.monotonic, sleep=clock.sleep)
    starts = []

    async def request() -> None:
        await gate.wait()
        starts.append(clock.value)

    await asyncio.gather(*(request() for _ in range(4)))
    assert starts == [0.0, 1.0, 2.0, 3.0]


async def test_defer_also_postpones_a_caller_already_sleeping() -> None:
    clock = FakeTime()
    entered, release = asyncio.Event(), asyncio.Event()
    sleeps: list[float] = []

    async def controlled_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            entered.set()
            await release.wait()
        await clock.sleep(seconds)

    gate = RequestGate(monotonic=clock.monotonic, sleep=controlled_sleep)
    await gate.wait()
    pending = asyncio.create_task(gate.wait())
    await entered.wait()
    gate.defer(3)
    release.set()
    await pending
    assert sleeps == [1.0, 2.0]
    assert clock.value == 3.0


@pytest.mark.parametrize("seconds", [-1, float("nan"), float("inf")])
def test_invalid_delay_is_rejected(seconds: float) -> None:
    with pytest.raises(ValueError):
        RequestGate().defer(seconds)


async def test_bootstrap_and_private_request_share_the_same_gate() -> None:
    clock = FakeTime()
    gate = RequestGate(monotonic=clock.monotonic, sleep=clock.sleep)
    starts: list[tuple[str, float]] = []
    exchange = PublicExchange(clock, starts)
    public = PublicAnalysisFeed(exchange=exchange, request_gate=gate)

    async def respond(request: httpx.Request) -> httpx.Response:
        starts.append(("balances", clock.value))
        assert int(request.headers["X-Revx-Timestamp"]) == 1_700_000_000_000 + int(
            clock.value * 1000
        )
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        private = private_client(transport, gate, clock)
        await asyncio.gather(public.markets(), private.balances())
    assert sorted(name for name, _ in starts) == ["balances", "currencies", "markets"]
    assert [stamp for _, stamp in starts] == [0.0, 1.0, 2.0]
    await public.close()


async def test_private_429_defers_public_client_without_retrying_the_post() -> None:
    clock = FakeTime()
    gate = RequestGate(monotonic=clock.monotonic, sleep=clock.sleep)
    starts: list[tuple[str, float]] = []
    public = PublicAnalysisFeed(exchange=PublicExchange(clock, starts), request_gate=gate)

    async def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        starts.append(("order", clock.value))
        return httpx.Response(429, headers={"Retry-After": "2500"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        private = private_client(transport, gate, clock)
        with pytest.raises(RevolutXClientError) as raised:
            await private.submit_limit(
                "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "BTC/EUR",
                "buy",
                Decimal("0.0001"),
                Decimal("100000"),
            )
        assert raised.value.retry_after_seconds == 2.5
        await public.tickers()
    assert starts == [("order", 0.0), ("tickers", 2.5)]
    await public.close()


async def test_public_429_delays_both_its_retry_and_the_private_client() -> None:
    clock = FakeTime()
    gate = RequestGate(monotonic=clock.monotonic, sleep=clock.sleep)
    starts: list[tuple[str, float]] = []
    exchange = PublicExchange(clock, starts)
    exchange.failures = 1
    public = PublicAnalysisFeed(exchange=exchange, request_gate=gate)

    async def respond(request: httpx.Request) -> httpx.Response:
        starts.append(("balances", clock.value))
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        private = private_client(transport, gate, clock)
        await asyncio.gather(public.tickers(), private.balances())
    assert starts == [("tickers", 0.0), ("tickers", 2.5), ("balances", 3.5)]
    await public.close()
