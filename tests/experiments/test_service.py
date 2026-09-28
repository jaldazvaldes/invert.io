from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from invertio.config.settings import Settings
from invertio.core.clock import SimClock
from invertio.core.models import Bar
from invertio.experiments import service as service_module
from invertio.experiments.service import KEY, ExperimentsService
from invertio.persistence.db import create_engine_async, session_factory, upgrade_db
from invertio.simulation.repository import SimulationRepository


class PublicFeed:
    def __init__(self, clock: SimClock) -> None:
        self.clock = clock
        self.calls: list[str] = []
        self.delay_seconds = 0
        self.failure = False

    async def book(self, symbol: str) -> dict[str, Any]:
        self.calls.append(symbol)
        if self.failure:
            raise ConnectionError("public data unavailable")
        self.clock.set(self.clock.now() + timedelta(seconds=self.delay_seconds))
        return {
            "ts": self.clock.now().isoformat(),
            "bids": [["99.99", "100"]],
            "asks": [["100.01", "100"]],
        }


@pytest.fixture
async def setup(
    tmp_path: Path, t0: datetime, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Any]:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    upgrade_db(settings)
    db = create_engine_async(settings)
    sessions = session_factory(db)
    repository = SimulationRepository(sessions, key=KEY)
    legacy = SimulationRepository(sessions)
    await legacy.save({"untouched": "existing paper history"})
    clock = SimClock(t0)
    feed = PublicFeed(clock)
    decisions = {"enter": True, "exit": False, "ready": True, "atr": 1}

    def policy(ident: str, bars: list[Bar], now: datetime) -> dict[str, Any]:
        closed = [bar for bar in bars if bar.close_time <= now]
        return {
            **decisions,
            "bar_ts": closed[-1].close_time.isoformat() if closed else None,
            "entry_reason": "Test entry rule",
            "exit_reason": "Test exit rule",
            "indicators": {},
        }

    monkeypatch.setattr(service_module, "evaluate", policy)
    comparison = ExperimentsService(repository, feed, clock=clock)  # type: ignore[arg-type]
    await comparison.start()
    yield comparison, feed, clock, decisions, sessions, legacy
    await db.dispose()


def inputs(at: datetime, symbols: tuple[str, ...] = ("BTC/EUR",)) -> dict[str, Any]:
    rows, bars, markets = [], {}, {}
    for symbol in symbols:
        rows.append(
            {
                "market": f"revolutx:{symbol}",
                "quote_ts": at.isoformat(),
                "bar_ts": at.isoformat(),
                "quote_volume": 100000,
                "spread_pct": 0.02,
                # Base-score and 100-euro entry exclusions must not gate other rules.
                "score": 20,
                "state": "watching",
                "reasons": ["Nota inferior a 70", "Objetivo neto no positivo después de costes"],
                "opportunity_id": "unrelated-base-opportunity",
            }
        )
        bars[symbol] = [
            Bar("revolutx", symbol, "1m", at - timedelta(minutes=1), 100, 101, 99, 100, 5)
        ]
        markets[symbol] = {
            "active": True,
            "spot": True,
            "quote": "EUR",
            "precision": {"amount": 0.00001},
            "limits": {"amount": {"min": 0.00001}, "cost": {"min": 1}},
        }
    return {"rows": rows, "bars": bars, "books": {}, "markets": markets}


async def test_four_balances_share_activation_books_and_execution_time(setup: Any) -> None:
    comparison, feed, clock, _, sessions, legacy = setup
    started = clock.now()
    assert len(comparison.state["portfolios"]) == 4
    for portfolio in comparison.state["portfolios"].values():
        assert portfolio["cash_eur"] == "50"
        assert portfolio["started_at"] == started.isoformat()
    feed.delay_seconds = 2
    await comparison.cycle(**inputs(started, ("BTC/EUR", "ETH/EUR")), analysis_cycle=1)
    assert comparison.last_error is None
    assert feed.calls == ["BTC/EUR", "ETH/EUR"]
    execution = (started + timedelta(seconds=4)).isoformat()
    ledgers = []
    for portfolio in comparison.state["portfolios"].values():
        assert portfolio["last_cycle_at"] == execution
        assert len(portfolio["positions"]) == 2
        ledgers.append((portfolio["cash_eur"], portfolio["stats"]["equity_eur"]))
        for position in portfolio["positions"].values():
            assert position["opened_at"] == execution
            assert position["entry"] == "100.01"
            assert position["source_rule_version"].startswith("experiment-v1:")
    assert len(set(ledgers)) == 1
    assert await legacy.load() == {"untouched": "existing paper history"}
    async with sessions() as session:
        for table in ("orders", "fills", "signals", "manual_orders", "equity_snapshots"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0


async def test_reuses_fresh_public_book_once_for_all_portfolios(setup: Any) -> None:
    comparison, feed, clock, _, _, _ = setup
    current = inputs(clock.now())
    current["books"]["BTC/EUR"] = await feed.book("BTC/EUR")
    feed.calls.clear()
    await comparison.cycle(**current, analysis_cycle=1)
    assert feed.calls == []
    assert all(len(p["positions"]) == 1 for p in comparison.state["portfolios"].values())


@pytest.mark.parametrize("first_ineligible", [False, True])
async def test_identical_rules_use_same_candidate_priority_when_slots_are_full(
    setup: Any, first_ineligible: bool
) -> None:
    comparison, feed, clock, _, _, _ = setup
    symbols = tuple(f"COIN{index}/EUR" for index in range(6))
    current = inputs(clock.now(), symbols)
    if first_ineligible:
        current["markets"][symbols[0]]["limits"]["cost"]["min"] = 20
    await comparison.cycle(**current, analysis_cycle=1)
    assert len(feed.calls) == 6
    selections = [tuple(sorted(p["positions"])) for p in comparison.state["portfolios"].values()]
    assert all(len(selection) == 5 for selection in selections)
    assert len(set(selections)) == 1
    expected = symbols[1:] if first_ineligible else symbols[:5]
    assert selections[0] == expected


async def test_does_not_open_pre_activation_or_replayed_signals(setup: Any) -> None:
    comparison, feed, clock, decisions, _, _ = setup
    started = clock.now()
    old = inputs(started - timedelta(seconds=30))
    old["rows"][0]["quote_ts"] = started.isoformat()
    await comparison.cycle(**old, analysis_cycle=1)
    assert feed.calls == []
    assert all(not p["positions"] for p in comparison.state["portfolios"].values())
    clock.set(started + timedelta(seconds=30))
    await comparison.cycle(**inputs(clock.now()), analysis_cycle=2)
    assert all(len(p["positions"]) == 1 for p in comparison.state["portfolios"].values())
    snapshot = copy.deepcopy(comparison.state)
    await comparison.cycle(**inputs(clock.now()), analysis_cycle=2)
    assert comparison.state == snapshot
    restarted = ExperimentsService(comparison.repository, feed, clock=clock)
    await restarted.start()
    await restarted.cycle(**inputs(clock.now()), analysis_cycle=2)
    assert restarted.state == snapshot
    # A new scheduler cycle with the same bar may value holdings, never buy them again.
    decisions["enter"] = True
    await restarted.cycle(**inputs(clock.now()), analysis_cycle=3)
    assert all(
        len([d for d in p["decisions"] if d["action"] == "buy"]) == 1
        for p in restarted.state["portfolios"].values()
    )


async def test_reentry_waits_for_false_condition_and_another_new_closed_bar(setup: Any) -> None:
    comparison, _, clock, decisions, _, _ = setup
    await comparison.cycle(**inputs(clock.now()), analysis_cycle=1)
    decisions.update(exit=True)
    clock.set(clock.now() + timedelta(minutes=1))
    await comparison.cycle(**inputs(clock.now()), analysis_cycle=2)
    assert all(len(p["trades"]) == 1 for p in comparison.state["portfolios"].values())
    decisions.update(exit=False)
    clock.set(clock.now() + timedelta(minutes=1))
    await comparison.cycle(**inputs(clock.now()), analysis_cycle=3)
    assert all(not p["positions"] for p in comparison.state["portfolios"].values())
    decisions.update(enter=False)
    clock.set(clock.now() + timedelta(minutes=1))
    await comparison.cycle(**inputs(clock.now()), analysis_cycle=4)
    # Re-evaluating the identical closed bar cannot create a new entry.
    decisions.update(enter=True)
    await comparison.cycle(**inputs(clock.now()), analysis_cycle=5)
    assert all(not p["positions"] for p in comparison.state["portfolios"].values())
    clock.set(clock.now() + timedelta(minutes=1))
    await comparison.cycle(**inputs(clock.now()), analysis_cycle=6)
    assert all(len(p["positions"]) == 1 for p in comparison.state["portfolios"].values())
    assert all(len(p["trades"]) == 1 for p in comparison.state["portfolios"].values())


async def test_failure_to_save_publishes_none_of_the_four_and_retry_is_exactly_once(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    comparison, feed, clock, _, _, _ = setup
    original = copy.deepcopy(comparison.state)
    save = comparison.repository.save

    async def fail_save(_: dict[str, Any]) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(comparison.repository, "save", fail_save)
    await comparison.cycle(**inputs(clock.now()), analysis_cycle=1)
    assert comparison.last_error
    assert comparison.state == original
    assert await comparison.repository.load() == original
    monkeypatch.setattr(comparison.repository, "save", save)
    await comparison.cycle(**inputs(clock.now()), analysis_cycle=1)
    assert comparison.last_error is None
    assert all(len(p["positions"]) == 1 for p in comparison.state["portfolios"].values())
    assert comparison.state == await comparison.repository.load()
    calls = len(feed.calls)
    await comparison.cycle(**inputs(clock.now()), analysis_cycle=1)
    assert len(feed.calls) == calls


async def test_book_loss_holds_cash_and_recovery_exits_at_current_time(setup: Any) -> None:
    comparison, feed, clock, _, _, _ = setup
    await comparison.cycle(**inputs(clock.now()), analysis_cycle=1)
    cash = {key: p["cash_eur"] for key, p in comparison.state["portfolios"].items()}
    feed.failure = True
    clock.set(clock.now() + timedelta(minutes=1))
    await comparison.cycle(**inputs(clock.now()), analysis_cycle=2)
    for key, portfolio in comparison.state["portfolios"].items():
        assert portfolio["cash_eur"] == cash[key]
        assert portfolio["stats"]["equity_eur"] is None
        assert not portfolio["trades"]
        assert portfolio["positions"]["BTC/EUR"]["status"] == "waiting_data"
    assert all(s["rank"] is None for s in comparison.status(True)["strategies"])
    feed.failure = False
    clock.set(clock.now() + timedelta(minutes=1))
    await comparison.cycle(**inputs(clock.now()), analysis_cycle=3)
    for portfolio in comparison.state["portfolios"].values():
        assert not portfolio["positions"]
        assert len(portfolio["trades"]) == 1
        assert portfolio["trades"][0]["had_data_gap"]
        assert portfolio["trades"][0]["closed_at"] == clock.now().isoformat()


async def test_atr_change_does_not_move_levels_or_inherit_base_target_veto(setup: Any) -> None:
    comparison, _, clock, decisions, _, _ = setup
    await comparison.cycle(**inputs(clock.now()), analysis_cycle=1)
    original = {
        key: copy.deepcopy(p["positions"]["BTC/EUR"])
        for key, p in comparison.state["portfolios"].items()
    }
    decisions.update(atr=0.000001, enter=False)
    clock.set(clock.now() + timedelta(minutes=1))
    await comparison.cycle(**inputs(clock.now()), analysis_cycle=2)
    for key, portfolio in comparison.state["portfolios"].items():
        assert portfolio["positions"]["BTC/EUR"] == original[key]
        assert not portfolio["trades"]


async def test_holding_missing_from_selected_rows_is_not_silently_kept(setup: Any) -> None:
    comparison, _, clock, _, _, _ = setup
    await comparison.cycle(**inputs(clock.now()), analysis_cycle=1)
    clock.set(clock.now() + timedelta(minutes=1))
    current = inputs(clock.now())
    current["rows"] = []
    await comparison.cycle(**current, analysis_cycle=2)
    for portfolio in comparison.state["portfolios"].values():
        assert not portfolio["positions"]
        assert portfolio["trades"][0]["had_data_gap"]
        assert portfolio["trades"][0]["closed_at"] == clock.now().isoformat()


async def test_restart_with_changed_definitions_does_not_reset_capital(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    comparison, feed, clock, _, _, _ = setup
    await comparison.cycle(**inputs(clock.now()), analysis_cycle=1)
    existing = copy.deepcopy(comparison.state)
    definitions = copy.deepcopy(existing["definitions"])
    definitions[0]["version"] = "a-different-rule-version"
    monkeypatch.setattr(service_module, "definitions", lambda: definitions)
    restarted = ExperimentsService(comparison.repository, feed, clock=clock)
    with pytest.raises(ValueError, match="otra versión"):
        await restarted.start()
    assert restarted.state is None
    assert await comparison.repository.load() == existing


async def test_status_has_net_cost_metrics_and_stale_positions_are_unranked(setup: Any) -> None:
    comparison, _, clock, _, _, _ = setup
    await comparison.cycle(**inputs(clock.now()), analysis_cycle=1)
    report = comparison.status(True)
    assert len(report["strategies"]) == 4
    assert all(s["rank"] == 1 for s in report["strategies"])
    for strategy in report["strategies"]:
        stats = strategy["stats"]
        assert Decimal(stats["net_pnl_eur"]) < 0
        assert Decimal(stats["fees_eur"]) > 0
        assert stats["closed_trades"] == 0
        assert stats["win_rate_pct"] is None
    clock.set(clock.now() + timedelta(seconds=121))
    stale = comparison.status(True)
    assert stale["state"] == "waiting_data"
    assert all(s["rank"] is None for s in stale["strategies"])
    assert all(s["stats"]["equity_eur"] is None for s in stale["strategies"])
