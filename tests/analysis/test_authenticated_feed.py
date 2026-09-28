from __future__ import annotations

import asyncio
import base64
import copy
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import invertio.analysis.authenticated_feed as feed_module
import invertio.manual.client as client_module
from invertio.analysis.authenticated_feed import AuthenticatedAnalysisFeed
from invertio.analysis.config import AnalysisConfig
from invertio.analysis.market import estimate_costs
from invertio.data.throttle import RequestGate
from invertio.manual.client import RevolutXClient, RevolutXClientError

T0 = datetime(2026, 9, 28, 12, tzinfo=UTC)
T0_MS = int(T0.timestamp() * 1000)
NOW_MS = T0_MS + 3000 * 60_000
PAIR = {
    "base": "BTC",
    "quote": "EUR",
    "base_step": "0.00000001",
    "quote_step": "0.01",
    "min_order_size": "0.00001",
    "max_order_size": "9000",
    "min_order_size_quote": "1",
    "status": "active",
}


def candle(start: int, **overrides: Any) -> dict[str, Any]:
    return {
        "start": start,
        "open": "100",
        "high": "101",
        "low": "99",
        "close": "100",
        "volume": "2",
        **overrides,
    }


class FakePublic:
    def __init__(self) -> None:
        self.options: dict[str, Any] = {}
        self.calls: list[str] = []
        self.closed = False
        self.configuration = {
            "BTC/EUR": {
                "base": "BTC",
                "quote": "EUR",
                "spot": True,
                "active": True,
                "info": copy.deepcopy(PAIR),
            }
        }

    async def load_markets(self, *, reload: bool) -> dict[str, Any]:
        assert reload
        self.calls.append("markets")
        return copy.deepcopy(self.configuration)

    async def fetch_tickers(self) -> dict[str, Any]:
        self.calls.append("tickers")
        return {
            "BTC/EUR": {
                "bid": 99.9,
                "ask": 100.0,
                "timestamp": T0_MS,
                "info": {"quote_volume_24h": "500000"},
            }
        }

    async def close(self) -> None:
        self.closed = True


class Clock:
    def __init__(self) -> None:
        self.time = 100.0

    def monotonic(self) -> float:
        return self.time

    async def sleep(self, delay: float) -> None:
        assert delay >= 0
        self.time += delay


class Harness:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.requests: list[httpx.Request] = []
        self.request_times: list[float] = []
        self.responses: list[httpx.Response] = []
        self.configuration = {"BTC/EUR": copy.deepcopy(PAIR)}
        self.candle_payload: Any = None
        self.book_payload: Any = {
            "data": {"asks": [{"p": "100", "q": "10"}], "bids": [{"p": "99.9", "q": "10"}]},
            "metadata": {"timestamp": T0_MS - 500},
        }
        self.key = Ed25519PrivateKey.generate()
        self.public = FakePublic()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        self.client = RevolutXClient(
            "k" * 64,
            self.key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
            client=self.http,
            timestamp_ms=lambda: NOW_MS,
            request_interval=0.2,
        )
        gate = RequestGate(monotonic=clock.monotonic, sleep=clock.sleep)
        self.feed = AuthenticatedAnalysisFeed(
            AnalysisConfig(), self.client, exchange=self.public, request_gate=gate
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert not request.content
        assert "region" not in request.url.params
        path, _, query = request.url.raw_path.partition(b"?")
        self.key.public_key().verify(
            base64.b64decode(request.headers["X-Revx-Signature"]),
            str(NOW_MS).encode() + b"GET" + path + query,
        )
        self.requests.append(request)
        self.request_times.append(self.clock.monotonic())
        if self.responses:
            return self.responses.pop(0)
        if request.url.path == "/api/1.0/configuration/pairs":
            return httpx.Response(200, json=self.configuration)
        if request.url.path == "/api/1.0/order-book/BTC-EUR":
            return httpx.Response(200, json=self.book_payload)
        assert request.url.path == "/api/1.0/candles/BTC-EUR"
        if self.candle_payload is not None:
            return httpx.Response(200, json=self.candle_payload)
        since, until = int(request.url.params["since"]), int(request.url.params["until"])
        return httpx.Response(
            200,
            json={
                "data": [candle(stamp) for stamp in range(since, until + 1, 60_000)],
                "metadata": {"timestamp": NOW_MS},
            },
        )


@pytest.fixture
async def harness(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    clock = Clock()
    # Replace each module's binding, not the global event-loop clock/sleep.
    monkeypatch.setattr(feed_module, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(
        feed_module, "asyncio", SimpleNamespace(Lock=asyncio.Lock, sleep=clock.sleep)
    )
    monkeypatch.setattr(client_module, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(
        client_module, "asyncio", SimpleNamespace(Lock=asyncio.Lock, sleep=clock.sleep)
    )
    result = Harness(clock)
    await result.feed.markets()
    yield result
    await result.feed.close()
    await result.http.aclose()


async def test_only_signed_market_gets_with_public_eea_tickers(harness: Harness) -> None:
    assert harness.feed.data_access == "authenticated"
    assert harness.feed.request_interval_seconds == 0.2
    assert harness.feed.market_verification == {
        "public_eur": 1,
        "verified_eur": 1,
        "unavailable": {},
    }
    rows = await harness.feed.bars("BTC/EUR", T0, T0 + timedelta(minutes=3, seconds=15))
    assert len(rows) == 3
    assert all(
        row.venue == "revolutx" and row.timeframe == "1m" and row.symbol == "BTC/EUR"
        for row in rows
    )
    request = harness.requests[-1]
    assert dict(request.url.params) == {
        "interval": "1",
        "since": str(T0_MS),
        "until": str(T0_MS + 195_000 - 1),
    }
    assert harness.public.options["region"] == "EEA"
    tickers = await harness.feed.tickers()
    assert tickers["BTC/EUR"]["quote_volume"] == 500000
    assert harness.public.calls == ["markets", "tickers"]
    assert len(harness.requests) == 2


async def test_bounded_two_page_history_keeps_chronological_closed_candles(
    harness: Harness,
) -> None:
    rows = await harness.feed.bars("BTC/EUR", T0, T0 + timedelta(days=100))
    assert len(rows) == 2000
    assert rows[-1].close_time == T0 + timedelta(minutes=2000)
    assert len(harness.requests) == 3
    requests = harness.requests[1:]
    assert [int(req.url.params["since"]) for req in requests] == [T0_MS, T0_MS + 1000 * 60_000]
    assert all(
        int(req.url.params["until"]) - int(req.url.params["since"]) == 1000 * 60_000 - 1
        for req in requests
    )


async def test_rejects_future_open_unaligned_invalid_and_negative_bars(harness: Harness) -> None:
    harness.candle_payload = {
        "data": [
            candle(T0_MS + 60_000, volume="0"),
            candle(T0_MS),
            candle(T0_MS + 120_000),  # Not closed yet.
            candle(T0_MS - 60_000),
            candle(T0_MS + 1),
            candle(T0_MS, high="99"),
            candle(T0_MS, low="101"),
            candle(T0_MS, open="0"),
            candle(T0_MS, close="NaN"),
            candle(T0_MS, volume="-1"),
            candle(T0_MS, high="Infinity"),
            candle(T0_MS, volume=True),
            candle(True),
            "invalid row",
        ],
        "metadata": {"timestamp": NOW_MS},
    }
    rows = await harness.feed.bars("BTC/EUR", T0, T0 + timedelta(minutes=2, seconds=30))
    assert [row.open_time for row in rows] == [T0, T0 + timedelta(minutes=1)]
    assert rows[-1].volume == 0


async def test_source_timestamp_never_makes_open_candles_look_closed(harness: Harness) -> None:
    harness.candle_payload = {
        "data": [candle(T0_MS), candle(T0_MS + 60_000)],
        "metadata": {"timestamp": T0_MS + 90_000},
    }
    rows = await harness.feed.bars("BTC/EUR", T0, T0 + timedelta(minutes=10))
    assert len(rows) == 1


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"timestamp": True},
        {"timestamp": -1},
        {"timestamp": "not a date"},
        {"timestamp": 1.5},
        {"timestamp": 10**40},
        {"timestamp": NOW_MS, "region": "UK"},
    ],
)
async def test_invalid_metadata_is_not_replaced_with_now(harness: Harness, metadata: Any) -> None:
    harness.candle_payload = {"data": [candle(T0_MS)], "metadata": metadata}
    with pytest.raises(RevolutXClientError, match="respuesta"):
        await harness.feed.bars("BTC/EUR", T0, T0 + timedelta(minutes=2))


@pytest.mark.parametrize(
    "payload", [{"data": {}, "metadata": {"timestamp": NOW_MS}}, [candle(T0_MS)], {"data": []}]
)
async def test_invalid_wrapper_is_not_empty_history(harness: Harness, payload: Any) -> None:
    harness.candle_payload = payload
    with pytest.raises(RevolutXClientError, match="respuesta"):
        await harness.feed.bars("BTC/EUR", T0, T0 + timedelta(minutes=2))


async def test_book_preserves_exchange_timestamp_strings_and_cost_parity(harness: Harness) -> None:
    book = await harness.feed.book("BTC/EUR")
    assert book["ts"] == datetime.fromtimestamp((T0_MS - 500) / 1000, UTC).isoformat()
    assert book["timestamp_source"] == "exchange"
    assert book["asks"] == [["100", "10"]]
    actual = estimate_costs(book, AnalysisConfig(), 1)
    numeric = {**book, "asks": [[100.0, 10.0]], "bids": [[99.9, 10.0]]}
    assert actual == estimate_costs(numeric, AnalysisConfig(), 1)


async def test_five_per_second_limit_applies_to_books_bars_and_retries(harness: Harness) -> None:
    # Even a caller-provided client with a faster setting cannot exceed the feed cap.
    harness.client._request_interval = 0
    await harness.feed.book("BTC/EUR")
    await harness.feed.bars("BTC/EUR", T0, T0 + timedelta(minutes=1))
    harness.responses.append(httpx.Response(429, headers={"Retry-After": "1100"}))
    await harness.feed.book("BTC/EUR")
    intervals = [
        end - start
        for start, end in zip(harness.request_times, harness.request_times[1:], strict=False)
    ]
    assert all(interval >= 0.199999 for interval in intervals)
    assert intervals[-1] >= 1.099999


async def test_429_is_bounded_and_other_http_failures_are_not_retried(harness: Harness) -> None:
    harness.responses = [httpx.Response(429, headers={"Retry-After": "250"}) for _ in range(4)]
    with pytest.raises(RevolutXClientError) as raised:
        await harness.feed.book("BTC/EUR")
    assert raised.value.status_code == 429
    assert len(harness.responses) == 1
    harness.responses = [httpx.Response(401)]
    before = len(harness.requests)
    with pytest.raises(RevolutXClientError) as raised:
        await harness.feed.book("BTC/EUR")
    assert raised.value.status_code == 401
    assert len(harness.requests) == before + 1


async def test_long_429_aborts_immediately_but_defers_next_cycle(harness: Harness) -> None:
    harness.responses = [httpx.Response(429, headers={"Retry-After": "120000"})]
    before = len(harness.requests)
    with pytest.raises(RevolutXClientError):
        await harness.feed.book("BTC/EUR")
    assert len(harness.requests) == before + 1
    failed_at = harness.request_times[-1]
    with pytest.raises(RevolutXClientError) as raised:
        await harness.feed.book("BTC/EUR")
    assert raised.value.retry_after_seconds == pytest.approx(120)
    assert len(harness.requests) == before + 1
    harness.clock.time += 120
    await harness.feed.book("BTC/EUR")
    assert harness.request_times[-1] - failed_at == pytest.approx(120)


@pytest.mark.parametrize("symbol", ["BTC/EUR?secret=1", "BTC/USD", "BTC-EUR", "DOGE/EUR"])
async def test_invalid_or_unverified_market_does_not_send_authenticated_requests(
    harness: Harness, symbol: str
) -> None:
    before = len(harness.requests)
    with pytest.raises(RevolutXClientError):
        await harness.feed.bars(symbol, T0, T0 + timedelta(minutes=1))
    with pytest.raises(RevolutXClientError):
        await harness.feed.book(symbol)
    assert len(harness.requests) == before


async def test_configuration_mismatch_disables_auth_market_without_adopting_account_pairs(
    harness: Harness,
) -> None:
    harness.configuration["BTC/EUR"]["base_step"] = "0.00001"
    harness.configuration["ETH/EUR"] = {**PAIR, "base": "ETH"}
    markets = await harness.feed.markets()
    assert markets["BTC/EUR"]["active"] is False
    assert "ETH/EUR" not in markets
    assert harness.feed.market_verification["verified_eur"] == 0
    assert (
        harness.feed.market_verification["unavailable"]["BTC/EUR"]
        == markets["BTC/EUR"]["analysis_data_reason"]
    )
    with pytest.raises(RevolutXClientError):
        await harness.feed.book("BTC/EUR")


async def test_auth_inactive_market_is_inactive_but_available_to_follow_holdings(
    harness: Harness,
) -> None:
    harness.configuration["BTC/EUR"]["status"] = "inactive"
    markets = await harness.feed.markets()
    assert markets["BTC/EUR"]["active"] is False
    assert (await harness.feed.book("BTC/EUR"))["asks"]


async def test_naive_or_negative_time_rejected_and_empty_window_does_not_request(
    harness: Harness,
) -> None:
    before = len(harness.requests)
    with pytest.raises(ValueError, match="zona horaria"):
        await harness.feed.bars("BTC/EUR", T0.replace(tzinfo=None), T0)
    with pytest.raises(RevolutXClientError):
        await harness.feed.bars("BTC/EUR", datetime(1960, 1, 1, tzinfo=UTC), T0)
    assert await harness.feed.bars("BTC/EUR", T0, T0) == []
    assert len(harness.requests) == before


async def test_closing_feed_closes_both_clients(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed = []

    async def close_client() -> None:
        closed.append(True)

    monkeypatch.setattr(harness.client, "close", close_client)
    await harness.feed.close()
    assert closed == [True]
    assert harness.public.closed
