from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from invertio.analysis.config import AnalysisConfig
from invertio.analysis.market import select_markets
from invertio.analysis.repository import AnalysisRepository
from invertio.analysis.service import AnalysisService
from invertio.config.settings import Settings
from invertio.core.clock import SimClock
from invertio.core.models import Bar
from invertio.data.store import BarStore
from invertio.persistence.db import create_engine_async, session_factory, upgrade_db
from invertio.strategies.score import ScoreResult


def catalog() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    symbols = [f"COIN{index:02}/EUR" for index in range(57)]
    symbols += ["USDC/EUR", "WIDE/EUR", "UNKNOWN/EUR", "ZERO/EUR"]
    markets = {
        symbol: {"base": symbol.split("/")[0], "quote": "EUR", "active": True, "spot": True}
        for symbol in symbols
    }
    markets.update(
        {
            "OLD/EUR": {"base": "OLD", "quote": "EUR", "active": False, "spot": True},
            "BTC/USD": {"base": "BTC", "quote": "USD", "active": True, "spot": True},
            "FUT/EUR": {"base": "FUT", "quote": "EUR", "active": True, "spot": False},
        }
    )
    tickers = {
        symbol: {"bid": 99.99, "ask": 100.01, "quote_volume": 100_000 - index}
        for index, symbol in enumerate(markets)
    }
    tickers["WIDE/EUR"]["ask"] = 102
    tickers["UNKNOWN/EUR"]["quote_volume"] = None
    tickers["ZERO/EUR"]["quote_volume"] = 0
    return markets, tickers


def test_all_eur_observes_61_pairs_even_when_entry_filters_exclude_them() -> None:
    markets, tickers = catalog()
    selected, excluded = select_markets(
        markets, tickers, AnalysisConfig(market_scope="all_eur"), []
    )
    expected = {s for s, m in markets.items() if m["active"] and m["spot"] and m["quote"] == "EUR"}
    assert set(selected) == expected
    assert len(selected) == len(set(selected)) == 61
    reasons = {row["symbol"]: row["reasons"] for row in excluded}
    assert "Stablecoin excluida" in reasons["USDC/EUR"]
    assert "Spread superior al límite" in reasons["WIDE/EUR"]
    assert "Volumen EUR desconocido o nulo" in reasons["UNKNOWN/EUR"]
    assert "Volumen EUR desconocido o nulo" in reasons["ZERO/EUR"]
    assert all("Fuera de las plazas por volumen" not in row["reasons"] for row in excluded)


def test_all_eur_keeps_every_held_market_and_deduplicates_beyond_twenty_slots() -> None:
    markets, tickers = catalog()
    held = [f"COIN{index:02}/EUR" for index in range(25)] + ["OLD/EUR", "OLD/EUR"]
    selected, excluded = select_markets(
        markets, tickers, AnalysisConfig(market_scope="all_eur", max_markets=1), held
    )
    assert set(held) <= set(selected)
    assert len(selected) == len(set(selected)) == 62
    assert next(row for row in excluded if row["symbol"] == "OLD/EUR")["held"] is True


def test_default_scope_still_limits_selection_to_twenty_eligible_pairs() -> None:
    markets, tickers = catalog()
    config = AnalysisConfig()
    assert config.market_scope == "top"
    selected, excluded = select_markets(markets, tickers, config, [])
    assert selected == [f"COIN{index:02}/EUR" for index in range(20)]
    assert {"USDC/EUR", "WIDE/EUR", "UNKNOWN/EUR", "ZERO/EUR"}.isdisjoint(selected)
    assert any(row["reasons"] == ["Fuera de las plazas por volumen"] for row in excluded)


class CoverageFeed:
    data_access = "authenticated"
    request_interval_seconds = 0.2

    def __init__(self, clock: SimClock) -> None:
        self.clock = clock
        self.market_data, self.ticker_data = catalog()
        self.bar_calls: list[str] = []
        self.book_calls: list[str] = []

    async def markets(self) -> dict[str, dict[str, Any]]:
        return self.market_data

    async def tickers(self) -> dict[str, dict[str, Any]]:
        result = {}
        for symbol, ticker in self.ticker_data.items():
            bid, ask = ticker["bid"], ticker["ask"]
            result[symbol] = {
                **ticker,
                "spread_pct": (ask - bid) / ((ask + bid) / 2) * 100,
                "ts": self.clock.now().isoformat(),
            }
        return result

    async def bars(
        self, symbol: str, since: datetime, now: datetime, *, timeframe: str = "1m"
    ) -> list[Bar]:
        self.bar_calls.append(symbol)
        return [Bar("revolutx", symbol, "1m", now - timedelta(minutes=1), 100, 101, 99, 100, 5)]

    async def book(self, symbol: str) -> dict[str, Any]:
        self.book_calls.append(symbol)
        return {"ts": self.clock.now().isoformat(), "bids": [[99.99, 100]], "asks": [[100.01, 100]]}

    async def close(self) -> None:
        pass


@pytest.fixture
async def coverage_setup(tmp_path: Path, t0: datetime) -> AsyncIterator[Any]:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    upgrade_db(settings)
    db = create_engine_async(settings)
    repository = AnalysisRepository(session_factory(db))
    clock = SimClock(t0)
    feed = CoverageFeed(clock)
    service = AnalysisService(
        AnalysisConfig(market_scope="all_eur"),
        feed,
        repository,
        BarStore(tmp_path / "analysis-bars"),
        clock=clock,
    )
    try:
        yield service, feed, clock, repository
    finally:
        await service.close()
        await db.dispose()


async def test_all_pairs_get_candles_and_persisted_market_coverage(coverage_setup: Any) -> None:
    service, feed, clock, repository = coverage_setup
    await service.cycle()
    assert service.last_error is None
    assert len(feed.bar_calls) == len(set(feed.bar_calls)) == 61
    assert len(service.rows) == 61
    assert not feed.book_calls  # Incomplete indicators never become entry candidates.
    coverage = (await service.status())["coverage"]
    assert coverage == {
        "scope": "all_eur",
        "catalog_eur": 61,
        "observed": 61,
        "entry_eligible": 57,
        "excluded": 4,
        "data_access": "authenticated",
        "request_interval_seconds": 0.2,
    }
    assert (await repository.load_state())["service"]["coverage"] == coverage
    observations = await repository.observations(limit=100)
    assert len(observations) == 61
    assert all(row["bar_ts"] == clock.now().isoformat() for row in observations)
    assert any(row["market"] == "revolutx:USDC/EUR" for row in observations)
    assert await repository.opportunities() == []


async def test_observed_stablecoin_cannot_open_even_with_high_score_and_liquidity(
    coverage_setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, feed, clock, repository = coverage_setup
    feed.market_data = {"USDC/EUR": feed.market_data["USDC/EUR"]}
    feed.ticker_data = {"USDC/EUR": feed.ticker_data["USDC/EUR"]}

    def score(symbol: str, bars: list[Bar]) -> ScoreResult:
        return ScoreResult(
            venue="revolutx",
            symbol=symbol,
            ts=clock.now(),
            close=100,
            score=100,
            points={"tendencia": 100},
            max_points={"tendencia": 100},
            atr=1,
            atr_pct=1,
            tradable=True,
            stop_loss=98,
            take_profit=103,
        )

    monkeypatch.setattr(service, "_score", score)
    await service.cycle()
    assert service.last_error is None
    assert service.selected == ["USDC/EUR"]
    assert service.rows[0]["score"] == 100
    assert any("Stablecoin excluida" in reason for reason in service.rows[0]["reasons"])
    assert service.rows[0]["state"] not in {"eligible", "open"}
    assert await repository.opportunities() == []
    assert feed.book_calls == []
