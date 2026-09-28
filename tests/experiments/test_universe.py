from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from invertio.config.settings import Settings
from invertio.core.clock import SimClock
from invertio.core.models import Bar
from invertio.experiments import service as service_module
from invertio.experiments.service import KEY, ExperimentsService, _filters
from invertio.persistence.db import create_engine_async, session_factory, upgrade_db
from invertio.simulation.repository import SimulationRepository


class UniverseFeed:
    def __init__(self, clock: SimClock) -> None:
        self.clock = clock
        self.calls: list[str] = []

    async def book(self, symbol: str) -> dict[str, Any]:
        self.calls.append(symbol)
        return {
            "ts": self.clock.now().isoformat(),
            "bids": [["99.99", "100"]],
            "asks": [["100.01", "100"]],
        }


def universe(at: datetime, symbols: tuple[str, ...]) -> dict[str, Any]:
    rows, bars, markets = [], {}, {}
    for symbol in symbols:
        rows.append(
            {
                "market": f"revolutx:{symbol}",
                "quote_ts": at.isoformat(),
                "bar_ts": at.isoformat(),
                "quote_volume": 100000,
                "spread_pct": 0.02,
            }
        )
        bars[symbol] = [
            Bar("revolutx", symbol, "1m", at - timedelta(minutes=1), 100, 101, 99, 100, 5)
        ]
        markets[symbol] = {
            "base": symbol.split("/")[0],
            "quote": "EUR",
            "active": True,
            "spot": True,
            "precision": {"amount": 0.00001},
            "limits": {"amount": {"min": 0.00001}, "cost": {"min": 1}},
        }
    return {"rows": rows, "bars": bars, "books": {}, "markets": markets}


@pytest.fixture
async def comparison_setup(
    tmp_path: Path, t0: datetime, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Any]:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    upgrade_db(settings)
    db = create_engine_async(settings)
    sessions = session_factory(db)
    repository = SimulationRepository(sessions, key=KEY)
    legacy = SimulationRepository(sessions)
    await legacy.save({"existing_history": ["preserve me"]})
    clock = SimClock(t0)
    feed = UniverseFeed(clock)
    decision = {"enter": True, "exit": False}

    def evaluate(ident: str, bars: list[Bar], now: datetime) -> dict[str, Any]:
        return {
            **decision,
            "ready": True,
            "atr": 1,
            "bar_ts": bars[-1].close_time.isoformat(),
            "entry_reason": "Test entry rule",
            "exit_reason": "Test exit rule",
            "indicators": {},
        }

    monkeypatch.setattr(service_module, "evaluate", evaluate)
    comparison = ExperimentsService(repository, feed, clock=clock)  # type: ignore[arg-type]
    await comparison.start()
    try:
        yield comparison, feed, clock, decision, sessions, legacy
    finally:
        await db.dispose()


@pytest.mark.parametrize("symbol", ["USDC/EUR", "EURC/EUR"])
def test_observing_a_stablecoin_does_not_make_it_entry_eligible(symbol: str, t0: datetime) -> None:
    data = universe(t0, (symbol, "BTC/EUR"))
    stable, regular = data["rows"]
    assert any(
        "Stablecoin excluida" in reason for reason in _filters(stable, data["markets"][symbol], t0)
    )
    assert _filters(regular, data["markets"]["BTC/EUR"], t0) == []


async def test_all_four_observe_stablecoins_but_only_simulate_eligible_markets(
    comparison_setup: Any,
) -> None:
    comparison, feed, clock, _, sessions, _ = comparison_setup
    await comparison.cycle(**universe(clock.now(), ("USDC/EUR", "BTC/EUR")), analysis_cycle=1)
    assert comparison.last_error is None
    assert feed.calls == ["BTC/EUR"]
    assert comparison.state["market_count"] == 2
    for ident, portfolio in comparison.state["portfolios"].items():
        assert set(portfolio["positions"]) == {"BTC/EUR"}
        stable = next(
            row for row in comparison.state["market_signals"][ident] if row["symbol"] == "USDC/EUR"
        )
        assert stable["enter"] is True
        assert any("Stablecoin excluida" in reason for reason in stable["data_reasons"])
    async with sessions() as session:
        for table in ("orders", "fills", "signals", "manual_orders"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0


async def test_expanding_to_61_markets_records_common_universe_without_resetting_portfolios(
    comparison_setup: Any,
) -> None:
    comparison, feed, clock, decision, _, legacy = comparison_setup
    started = clock.now().isoformat()
    await comparison.cycle(**universe(clock.now(), ("BTC/EUR",)), analysis_cycle=1)
    before = copy.deepcopy(comparison.state)
    assert all(len(p["positions"]) == 1 for p in before["portfolios"].values())
    decision["enter"] = False
    clock.set(clock.now() + timedelta(minutes=1))
    symbols = ("BTC/EUR", *(f"COIN{index:02}/EUR" for index in range(60)))
    await comparison.cycle(**universe(clock.now(), symbols), analysis_cycle=2)
    assert comparison.last_error is None
    after = comparison.state
    assert after["started_at"] == started
    assert after["experiment_id"] == before["experiment_id"]
    assert after["market_count"] == 61
    change = after["universe_history"][-1]
    assert change == {
        "at": clock.now().isoformat(),
        "previous_symbols": ["BTC/EUR"],
        "symbols": sorted(symbols),
    }
    for ident, portfolio in after["portfolios"].items():
        previous = before["portfolios"][ident]
        assert portfolio["started_at"] == previous["started_at"] == started
        assert portfolio["cash_eur"] == previous["cash_eur"]
        assert portfolio["positions"] == previous["positions"]
        assert portfolio["trades"] == previous["trades"]
        assert len(after["market_signals"][ident]) == 61
    assert await legacy.load() == {"existing_history": ["preserve me"]}

    # A different row order is not a new universe, and replay/restart cannot reset capital.
    count = len(after["universe_history"])
    clock.set(clock.now() + timedelta(minutes=1))
    await comparison.cycle(**universe(clock.now(), tuple(reversed(symbols))), analysis_cycle=3)
    assert len(comparison.state["universe_history"]) == count
    persisted = copy.deepcopy(comparison.state)
    restarted = ExperimentsService(comparison.repository, feed, clock=clock)
    await restarted.start()
    await restarted.cycle(**universe(clock.now(), symbols), analysis_cycle=3)
    assert restarted.state == persisted == await comparison.repository.load()


async def test_replacing_a_market_is_a_universe_change_even_when_count_is_equal(
    comparison_setup: Any,
) -> None:
    comparison, _, clock, decision, _, _ = comparison_setup
    decision["enter"] = False
    await comparison.cycle(**universe(clock.now(), ("BTC/EUR",)), analysis_cycle=1)
    count = len(comparison.state["universe_history"])
    clock.set(clock.now() + timedelta(minutes=1))
    await comparison.cycle(**universe(clock.now(), ("ETH/EUR",)), analysis_cycle=2)
    assert comparison.last_error is None
    assert len(comparison.state["universe_history"]) == count + 1
    assert comparison.state["universe_history"][-1]["previous_symbols"] == ["BTC/EUR"]
    assert comparison.state["universe_history"][-1]["symbols"] == ["ETH/EUR"]
    assert all(p["cash_eur"] == "50" for p in comparison.state["portfolios"].values())
