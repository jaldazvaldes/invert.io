from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from invertio.data.alpaca_history import DATA_URL, alpaca_timeframe, download_alpaca_bars
from invertio.data.ccxt_history import download_ohlcv
from invertio.data.sessions import in_us_regular_session

T0 = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
MS_5M = 5 * 60 * 1000


class FakeExchange:
    """Exchange ccxt de mentira: una vela cada 5 min salvo en los huecos indicados."""

    def __init__(self, exchange_id: str, missing: set[int] = frozenset()) -> None:  # type: ignore[assignment]
        self.id = exchange_id
        self.missing = missing
        self.calls: list[tuple[int, int]] = []

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int | None = None,
        limit: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[list[Any]]:
        assert since is not None and limit is not None and params is not None
        until = params["until"]
        self.calls.append((since, until))
        assert (until - since) // MS_5M < limit + 1, "la ventana supera el límite del exchange"
        rows: list[list[Any]] = []
        ts = since
        while ts <= until and len(rows) < limit:
            index = (ts - int(T0.timestamp() * 1000)) // MS_5M
            if index not in self.missing:
                rows.append([ts, 100 + index, 101 + index, 99 + index, 100.5 + index, 2.0])
            ts += MS_5M
        return rows


async def test_ccxt_download_paginates_skips_gaps_and_open_candle() -> None:
    exchange = FakeExchange("revolutx", missing={3, 4})
    now = T0 + timedelta(minutes=5 * 2500 + 2)  # la vela 2500 sigue abierta
    bars = await download_ohlcv(
        exchange, "BTC/EUR", "5m", T0, T0 + timedelta(days=30), venue="revolutx", now=now
    )
    assert len(exchange.calls) == 3  # ventanas de 1000 velas en Revolut X
    assert len(bars) == 2500 - 2  # 0..2499 menos los dos huecos
    assert bars[0].open_time == T0
    assert bars[-1].open_time == T0 + timedelta(minutes=5 * 2499)
    assert all(b.venue == "revolutx" for b in bars)


async def test_ccxt_download_uses_smaller_pages_for_okx() -> None:
    exchange = FakeExchange("myokx")
    bars = await download_ohlcv(
        exchange,
        "BTC/EUR",
        "5m",
        T0,
        T0 + timedelta(minutes=5 * 700),
        venue="revolutx",
        now=T0 + timedelta(days=10),
    )
    assert len(bars) == 700
    assert len(exchange.calls) == 3  # 300 + 300 + 100


def test_alpaca_timeframes() -> None:
    assert [alpaca_timeframe(t) for t in ("1m", "5m", "15m", "1h", "1d")] == [
        "1Min", "5Min", "15Min", "1Hour", "1Day",
    ]  # fmt: skip


def test_us_regular_session_handles_dst() -> None:
    # 14:30 UTC en enero = 9:30 en Nueva York (EST); en julio es 10:30 (EDT).
    assert in_us_regular_session(datetime(2026, 1, 5, 14, 30, tzinfo=UTC))
    assert not in_us_regular_session(datetime(2026, 1, 5, 14, 25, tzinfo=UTC))
    assert in_us_regular_session(datetime(2026, 7, 6, 13, 30, tzinfo=UTC))
    assert not in_us_regular_session(datetime(2026, 1, 10, 15, 0, tzinfo=UTC))  # sábado


def _alpaca_bar(ts: str, price: float) -> dict[str, Any]:
    return {"t": ts, "o": price, "h": price + 1, "l": price - 1, "c": price + 0.5, "v": 100}


@respx.mock
async def test_alpaca_download_paginates_and_filters_session() -> None:
    url = DATA_URL.format(symbol="AAPL")
    route = respx.get(url).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "bars": [
                        _alpaca_bar("2026-01-05T14:25:00Z", 200),  # preapertura: fuera
                        _alpaca_bar("2026-01-05T14:30:00Z", 201),
                    ],
                    "next_page_token": "abc",
                },
            ),
            httpx.Response(
                200,
                json={"bars": [_alpaca_bar("2026-01-05T14:35:00Z", 202)], "next_page_token": None},
            ),
        ]
    )
    async with httpx.AsyncClient() as client:
        bars = await download_alpaca_bars(
            client,
            api_key="k",
            api_secret="s",
            symbol="AAPL",
            timeframe="5m",
            start=datetime(2026, 1, 5, tzinfo=UTC),
            end=datetime(2026, 1, 6, tzinfo=UTC),
            venue="trading212",
            now=datetime(2026, 1, 7, tzinfo=UTC),
        )
    assert [b.close for b in bars] == [201.5, 202.5]
    assert route.calls[0].request.headers["APCA-API-KEY-ID"] == "k"
    assert route.calls[0].request.url.params["feed"] == "iex"
    assert route.calls[1].request.url.params["page_token"] == "abc"


@respx.mock
async def test_alpaca_bad_credentials() -> None:
    respx.get(DATA_URL.format(symbol="AAPL")).mock(return_value=httpx.Response(403))
    async with httpx.AsyncClient() as client:
        with pytest.raises(PermissionError, match="ALPACA_API_KEY"):
            await download_alpaca_bars(
                client,
                api_key="k",
                api_secret="s",
                symbol="AAPL",
                timeframe="5m",
                start=datetime(2026, 1, 5, tzinfo=UTC),
                end=datetime(2026, 1, 6, tzinfo=UTC),
                venue="trading212",
                now=datetime(2026, 1, 7, tzinfo=UTC),
            )
