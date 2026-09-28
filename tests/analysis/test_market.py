import asyncio
import math
import time
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import pytest
from ccxt.base.errors import RateLimitExceeded

from invertio.analysis.config import AnalysisConfig
from invertio.analysis.market import (
    PublicAnalysisFeed,
    _retry_after,
    estimate_costs,
    estimate_tracking_costs,
    normalize_ticker,
    select_markets,
)

T0 = datetime(2026, 9, 28, 12, tzinfo=UTC)


def market(base: str, **overrides: Any) -> dict[str, Any]:
    return {"base": base, "quote": "EUR", "active": True, "spot": True, **overrides}


def ticker(volume: float | None, **overrides: Any) -> dict[str, Any]:
    return {"bid": 99.99, "ask": 100.01, "quote_volume": volume, **overrides}


def test_selection_uses_eur_volume_reserves_held_and_explains_exclusions() -> None:
    markets = {
        "BTC/EUR": market("BTC"),
        "ETH/EUR": market("ETH"),
        "SMALL/EUR": market("SMALL"),
        "USDC/EUR": market("USDC"),
        "OLD/EUR": market("OLD", active=False),
        "NO/EUR": market("NO"),
        "ZERO/EUR": market("ZERO"),
        "WIDE/EUR": market("WIDE"),
        "BTC/USD": market("BTC", quote="USD"),
        "FUT/EUR": market("FUT", spot=False),
    }
    tickers = {symbol: ticker(100) for symbol in markets}
    tickers.update(
        {
            "BTC/EUR": ticker(2000),
            "ETH/EUR": ticker(1000),
            "SMALL/EUR": ticker(1),
            "NO/EUR": ticker(None),
            "ZERO/EUR": ticker(0),
            "WIDE/EUR": ticker(500, ask=102),
        }
    )
    selected, excluded = select_markets(
        markets, tickers, AnalysisConfig(max_markets=2), ["SMALL/EUR"]
    )
    assert selected == ["SMALL/EUR", "BTC/EUR"]
    reasons = {row["symbol"]: row["reasons"] for row in excluded}
    assert reasons["ETH/EUR"] == ["Fuera de las plazas por volumen"]
    assert "Stablecoin excluida" in reasons["USDC/EUR"]
    assert "Spread superior al límite" in reasons["WIDE/EUR"]
    assert "Volumen EUR desconocido o nulo" in reasons["NO/EUR"]
    assert "Volumen EUR desconocido o nulo" in reasons["ZERO/EUR"]
    assert len(selected) <= 20
    assert all(row["market"] == f"revolutx:{row['symbol']}" for row in excluded)


def test_held_invalid_market_still_reserves_slot_and_reports_failure() -> None:
    selected, excluded = select_markets(
        {"BTC/EUR": market("BTC", active=False), "ETH/EUR": market("ETH")},
        {"BTC/EUR": ticker(None), "ETH/EUR": ticker(10000)},
        AnalysisConfig(max_markets=1),
        ["BTC/EUR", "BTC/EUR"],
    )
    assert selected == ["BTC/EUR"]
    assert excluded[0]["held"] is True


def test_normalization_falls_back_to_raw_quote_volume_and_preserves_old_timestamp() -> None:
    result = normalize_ticker(
        {
            "bid": "100",
            "ask": "100.1",
            "quoteVolume": None,
            "info": {"quote_volume_24h": "123456.78"},
            "timestamp": int((T0 - timedelta(minutes=10)).timestamp() * 1000),
        },
        T0,
    )
    assert result["quote_volume"] == 123456.78
    assert result["ts"] == (T0 - timedelta(minutes=10)).isoformat()
    assert result["observed_at"] == T0.isoformat()
    assert result["timestamp_source"] == "exchange"
    assert normalize_ticker({}, T0)["timestamp_source"] == "observed"


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1, "invalid", True])
def test_invalid_market_numbers_never_become_fresh_valid_values(invalid: Any) -> None:
    result = normalize_ticker(
        {
            "bid": invalid,
            "ask": 100,
            "quoteVolume": invalid,
            "timestamp": invalid,
        },
        T0,
    )
    assert result["bid"] is None
    assert result["quote_volume"] is None
    assert result["spread_pct"] is None
    assert result["timestamp_source"] == "invalid"
    selected, _ = select_markets(
        {"BTC/EUR": market("BTC")}, {"BTC/EUR": result}, AnalysisConfig(), []
    )
    assert selected == []


def test_costs_use_book_fills_and_charge_spread_exactly_once() -> None:
    config = AnalysisConfig()
    result = estimate_costs({"asks": [[100.1, 10]], "bids": [[99.9, 10]]}, config, atr=1)
    assert result["eligible"]
    quantity = 100 / 100.1
    proceeds = quantity * 99.9
    expected_cost = 100 - proceeds + 0.09 + proceeds * 0.0009
    assert result["quantity"] == pytest.approx(quantity)
    assert result["entry"] == pytest.approx(100.1)
    assert result["stop"] == pytest.approx(98.1)
    assert result["target"] == pytest.approx(103.1)
    assert result["round_trip_cost_eur"] == pytest.approx(expected_cost)
    assert result["buy_slippage_pct"] == pytest.approx(0)
    assert result["sell_slippage_pct"] == pytest.approx(0)
    assert result["exit_price_factor"] == pytest.approx(0.999)
    expected_target_net = quantity * 103.1 * 0.999 * 0.9991 - 100.09
    assert result["target_net_pct"] == pytest.approx(expected_target_net)
    # Subtracting the full round-trip from target-entry would charge entry impact twice.
    wrong_net = (103.1 - 100.1) * quantity - expected_cost
    assert result["target_net_pct"] > wrong_net


def test_costs_fill_multiple_levels_on_both_sides() -> None:
    config = AnalysisConfig(max_spread_pct=1, max_slippage_pct=5)
    result = estimate_costs(
        {"asks": [[101, 3], [100, 0.5]], "bids": [[99, 0.5], [98, 10]]}, config, atr=2
    )
    quantity = 0.5 + 50 / 101
    assert result["entry"] == pytest.approx(100 / quantity)
    assert result["exit_estimate"] == pytest.approx((0.5 * 99 + (quantity - 0.5) * 98) / quantity)
    assert result["buy_slippage_pct"] > 0
    assert result["sell_slippage_pct"] > 0


def test_tracking_keeps_original_avax_target_when_new_entry_target_fails() -> None:
    config = AnalysisConfig()
    original = estimate_costs(
        {"asks": [[9.246, 100]], "bids": [[9.241, 100]]}, config, atr=0.008609167632541593
    )
    opportunity = {**original, "costs": original, "config": config.model_dump(mode="json")}
    current_book = {"asks": [[9.259, 100]], "bids": [[9.251, 2], [9.249, 100]]}
    new_entry = estimate_costs(current_book, config, atr=0.007423210866834667)
    tracking = estimate_tracking_costs(current_book, opportunity)
    assert not new_entry["eligible"]
    assert new_entry["target_net_pct"] < 0
    assert tracking["eligible"]
    assert tracking["target_net_pct"] > 0
    for key in ("target", "entry", "stop", "quantity", "entry_fee_eur"):
        assert tracking[key] == original[key]


def test_tracking_depth_uses_original_quantity_and_original_fees() -> None:
    config = AnalysisConfig(taker_fee_pct=0.5)
    original = estimate_costs({"asks": [[100, 10]], "bids": [[99.99, 10]]}, config, atr=1)
    opportunity = {**original, "costs": original, "config": config.model_dump(mode="json")}
    insufficient = {"asks": [[200, 10]], "bids": [[199.99, 0.75]]}
    assert estimate_costs(insufficient, config, atr=1)["quantity"] == 0.5
    tracking = estimate_tracking_costs(insufficient, opportunity)
    assert not tracking["eligible"]
    assert tracking["reasons"] == ["Profundidad insuficiente para vender la cantidad original"]

    tracking = estimate_tracking_costs({"asks": [[200, 10]], "bids": [[199.99, 10]]}, opportunity)
    expected_net = 103 * (199.99 / 199.995) * 0.995 - 100.5
    assert tracking["quantity"] == 1
    assert tracking["entry_fee_eur"] == 0.5
    assert tracking["target_net_pct"] == pytest.approx(expected_net)


@pytest.mark.parametrize(
    ("book", "atr", "expected"),
    [
        ({"asks": [], "bids": []}, 1, "Libro vacío"),
        ({"asks": [[100, 1]], "bids": [[101, 1]]}, 1, "Libro vacío"),
        ({"asks": [[100, 0.1]], "bids": [[99.9, 10]]}, 1, "Profundidad insuficiente para comprar"),
        ({"asks": [[100, 2]], "bids": [[99.9, 0.1]]}, 1, "Profundidad insuficiente para vender"),
        ({"asks": [[100, 0.1], [110, 10]], "bids": [[99.9, 10]]}, 10, "Deslizamiento de compra"),
        ({"asks": [[100, 10]], "bids": [[99.9, 0.1], [90, 10]]}, 10, "Deslizamiento de venta"),
        ({"asks": [[100, 10]], "bids": [[99.9, 10]]}, 0.01, "Objetivo neto no positivo"),
        ({"asks": [[100, 10]], "bids": [[99.9, 10]]}, 60, "Stop no positivo"),
        ({"asks": [[100, 10]], "bids": [[99.9, 10]]}, float("nan"), "ATR inválido"),
        ({"asks": [[100, float("inf")]], "bids": [[99.9, 10]]}, 1, "Libro vacío"),
    ],
)
def test_unusable_opportunities_explain_why(
    book: dict[str, Any], atr: float, expected: str
) -> None:
    result = estimate_costs(book, AnalysisConfig(), atr)
    assert not result["eligible"]
    assert any(expected in reason for reason in result["reasons"])
    assert all(math.isfinite(value) for value in result.values() if isinstance(value, float))


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Retry-After": "2000"}, 2),
        ({"Retry-After": "2s"}, 2),
        ({"retry-after": "1500ms"}, 1.5),
        ({"Retry-After-Ms": "1250"}, 1.25),
        ({"Retry-After": "Mon, 28 Sep 2026 12:00:03 GMT"}, 3),
        ({"Retry-After": "garbage"}, 2),
        ({}, 2),
    ],
)
def test_retry_after_units_and_dates(headers: dict[str, Any], expected: float) -> None:
    assert _retry_after(headers, T0) == expected


class FakeTransportExchange:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []
        self.windows: list[tuple[int, int]] = []
        self.last_response_headers: dict[str, str] = {}
        self.failures = 0
        self.closed = False
        self.active = True
        self.reloads: list[bool] = []
        self.cached_markets: dict[str, dict[str, Any]] | None = None

    async def fetch(self, path: str) -> dict[str, Any]:
        self.calls.append((path, time.monotonic()))
        if self.failures:
            self.failures -= 1
            self.last_response_headers = {"retry-after": "1100ms"}
            raise RateLimitExceeded("429")
        return {}

    async def load_markets(self, *, reload: bool = False) -> dict[str, dict[str, Any]]:
        self.reloads.append(reload)
        if self.cached_markets is not None and not reload:
            return self.cached_markets
        await asyncio.gather(self.fetch("currencies"), self.fetch("markets"))
        self.cached_markets = {"BTC/EUR": market("BTC", active=self.active)}
        return self.cached_markets

    async def fetch_tickers(self) -> dict[str, dict[str, Any]]:
        await self.fetch("tickers")
        return {"BTC/EUR": {"bid": 99.9, "ask": 100, "info": {"quote_volume_24h": "1000"}}}

    async def fetch_order_book(self, symbol: str) -> dict[str, Any]:
        await self.fetch("book")
        return {"bids": [[99.9, 10]], "asks": [[100, 10]], "timestamp": int(T0.timestamp() * 1000)}

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, *, since: int, limit: int, params: dict[str, Any]
    ) -> list[list[float]]:
        await self.fetch("bars")
        assert timeframe == "1m"
        self.windows.append((since, params["until"]))
        return [[ts, 100, 101, 99, 100, 10] for ts in range(since, params["until"] + 1, 60_000)]

    async def close(self) -> None:
        self.closed = True


async def test_shared_transport_limit_covers_bootstrap_and_concurrent_calls() -> None:
    exchange = FakeTransportExchange()
    feed = PublicAnalysisFeed(exchange=exchange)
    await feed.markets()
    tickers, book = await asyncio.gather(feed.tickers(), feed.book("BTC/EUR"))
    assert tickers["BTC/EUR"]["quote_volume"] == 1000
    assert book["ts"] == T0.isoformat()
    starts = [at for _, at in exchange.calls]
    assert len(starts) == 4
    assert all(b - a >= 0.99 for a, b in pairwise(starts))
    assert exchange.options["region"] == "EEA"  # type: ignore[attr-defined]
    await feed.close()
    assert exchange.closed


async def test_transport_retry_honors_milliseconds_and_has_finite_attempts() -> None:
    exchange = FakeTransportExchange()
    exchange.failures = 1
    feed = PublicAnalysisFeed(exchange=exchange)
    await feed.tickers()
    assert len(exchange.calls) == 2
    assert exchange.calls[1][1] - exchange.calls[0][1] >= 1.09
    await feed.close()


async def test_market_refresh_invalidates_ccxt_cache_to_observe_delisting() -> None:
    exchange = FakeTransportExchange()
    feed = PublicAnalysisFeed(exchange=exchange)
    assert (await feed.markets())["BTC/EUR"]["active"] is True
    exchange.active = False
    assert (await feed.markets())["BTC/EUR"]["active"] is False
    assert exchange.reloads == [True, True]
    await feed.close()


async def test_rate_limit_retry_stops_after_three_attempts() -> None:
    exchange = FakeTransportExchange()
    exchange.failures = 10
    feed = PublicAnalysisFeed(exchange=exchange)
    with pytest.raises(RateLimitExceeded):
        await feed.tickers()
    assert len(exchange.calls) == 3
    await feed.close()


async def test_bars_close_time_boundary_and_bounded_pagination() -> None:
    exchange = FakeTransportExchange()
    feed = PublicAnalysisFeed(exchange=exchange, page_cap=1)
    now = T0 + timedelta(minutes=3, seconds=15)
    bars = await feed.bars("BTC/EUR", T0, now)
    assert len(bars) == 3
    assert bars[-1].close_time == T0 + timedelta(minutes=3)
    assert all(bar.venue == "revolutx" and bar.timeframe == "1m" for bar in bars)
    bars = await feed.bars("BTC/EUR", T0, T0 + timedelta(days=100))
    assert len(bars) == 1000
    assert len(exchange.windows) == 2
    assert exchange.windows[-1][1] - exchange.windows[-1][0] < 1000 * 60_000
    await feed.close()
