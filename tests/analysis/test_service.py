from __future__ import annotations

import asyncio
import copy
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from invertio.analysis.config import AnalysisConfig
from invertio.analysis.notifications import NotificationDispatcher
from invertio.analysis.repository import AnalysisRepository
from invertio.analysis.service import AnalysisService
from invertio.config.settings import Settings
from invertio.core.clock import SimClock
from invertio.core.models import Bar
from invertio.data.store import BarStore
from invertio.persistence.db import create_engine_async, session_factory, upgrade_db
from invertio.persistence.models import EquitySnapshotRow
from invertio.strategies.score import ScoreResult


class FakeAnalysisFeed:
    """Public observations with controllable outages, timestamps and closed-minute bars."""

    def __init__(self, clock: SimClock) -> None:
        self.clock = clock
        self.origin = clock.now() - timedelta(minutes=250)
        self.symbols = ["BTC/EUR"]
        self.scores = {"BTC/EUR": 70.0, "ETH/EUR": 80.0}
        self.atr = 1.0
        self.quote_lag_seconds = 0
        self.bar_lag_minutes = 0
        self.bar_volume = 10.0
        self.include_open_bar = False
        self.failed_symbols: set[str] = set()
        self.tickers_failed = False
        self.closed = False

    async def markets(self) -> dict[str, dict[str, Any]]:
        return {
            symbol: {"quote": "EUR", "base": symbol.split("/")[0], "spot": True, "active": True}
            for symbol in self.symbols
        }

    async def tickers(self) -> dict[str, dict[str, Any]]:
        if self.tickers_failed:
            raise ConnectionError("public data unavailable")
        return {
            symbol: {
                "bid": 99.99,
                "ask": 100.01,
                "spread_pct": 0.02,
                "quote_volume": 100_000 / (index + 1),
                "ts": (self.clock.now() - timedelta(seconds=self.quote_lag_seconds)).isoformat(),
                "observed_at": self.clock.now().isoformat(),
                "timestamp_source": "exchange",
            }
            for index, symbol in enumerate(self.symbols)
        }

    async def bars(
        self, symbol: str, since: datetime, now: datetime, *, timeframe: str = "1m"
    ) -> list[Bar]:
        if symbol in self.failed_symbols:
            raise ConnectionError("public candles unavailable")
        end = now - timedelta(minutes=self.bar_lag_minutes)
        count = int((end - self.origin).total_seconds() // 60) + int(self.include_open_bar)
        result = []
        for index in range(count):
            start = self.origin + timedelta(minutes=index)
            if start < since:
                continue
            result.append(
                Bar(
                    "revolutx",
                    symbol,
                    "1m",
                    start,
                    open=100.0,
                    high=100.5,
                    low=99.5,
                    close=100.0,
                    volume=self.bar_volume,
                )
            )
        return result

    async def book(self, symbol: str) -> dict[str, Any]:
        return {
            "bids": [[99.99, 100]],
            "asks": [[100.01, 100]],
            "ts": self.clock.now().isoformat(),
            "observed_at": self.clock.now().isoformat(),
            "timestamp_source": "exchange",
        }

    async def close(self) -> None:
        self.closed = True

    def score(self, symbol: str, bars: list[Bar]) -> ScoreResult | None:
        if not bars:
            return None
        value = self.scores[symbol]
        return ScoreResult(
            venue="revolutx",
            symbol=symbol,
            ts=bars[-1].close_time,
            close=bars[-1].close,
            score=value,
            points={"tendencia": value},
            max_points={"tendencia": 100},
            atr=self.atr,
            atr_pct=self.atr,
            tradable=True,
            stop_loss=100 - self.atr * 2,
            take_profit=100 + self.atr * 3,
        )


@dataclass
class Harness:
    settings: Settings
    clock: SimClock
    feed: FakeAnalysisFeed
    repository: AnalysisRepository
    store: BarStore
    service: AnalysisService

    def advance(self, minutes: int = 1) -> None:
        self.clock.set(self.clock.now() + timedelta(minutes=minutes))

    def restart(self) -> AnalysisService:
        return AnalysisService(
            AnalysisConfig(), self.feed, self.repository, self.store, clock=self.clock
        )


@pytest.fixture
async def harness(tmp_path: Path, t0: datetime) -> AsyncIterator[Harness]:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    upgrade_db(settings)
    engine = create_engine_async(settings)
    sessions = session_factory(engine)
    async with sessions.begin() as session:
        session.add(
            EquitySnapshotRow(
                mode="paper",
                ts=t0,
                venue="revolutx",
                currency="EUR",
                equity=Decimal("100.25"),
                cash=Decimal("75.01"),
            )
        )
    clock = SimClock(t0)
    feed = FakeAnalysisFeed(clock)
    repository = AnalysisRepository(sessions)
    store = BarStore(tmp_path / "analysis-bars")
    service = AnalysisService(AnalysisConfig(), feed, repository, store, clock=clock)
    try:
        yield Harness(settings, clock, feed, repository, store, service)
    finally:
        await service.close()
        await engine.dispose()


def deterministic(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness.service, "_score", harness.feed.score)


async def test_entry_at_threshold_freezes_levels_without_duplicate_after_restart(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deterministic(harness, monkeypatch)
    await harness.service.cycle()
    opportunities = await harness.repository.opportunities()
    assert len(opportunities) == 1
    original = opportunities[0]
    assert original["status"] == "open" and original["score"] == 70
    assert original["entry"] == pytest.approx(100.01)
    assert original["stop"] == pytest.approx(98.01)
    assert original["target"] == pytest.approx(103.01)
    assert original["costs"]["eligible"]
    harness.feed.atr = 2
    harness.advance()
    await harness.service.cycle()
    harness.advance()
    restarted = harness.restart()
    monkeypatch.setattr(restarted, "_score", harness.feed.score)
    await restarted.cycle()
    after = await harness.repository.opportunities()
    assert len(after) == 1
    assert after[0]["id"] == original["id"]
    assert {key: after[0][key] for key in ("entry", "stop", "target")} == {
        key: original[key] for key in ("entry", "stop", "target")
    }
    notices = await harness.repository.notifications(original["id"])
    assert [notice["kind"] for notice in notices] == ["start"]


async def test_new_entry_cost_veto_does_not_close_a_viable_frozen_target(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deterministic(harness, monkeypatch)
    await harness.service.cycle()
    original = (await harness.repository.opportunities())[0]
    harness.feed.atr = 0.01
    harness.advance()
    await harness.service.cycle()
    saved = await harness.repository.opportunity(original["id"])
    assert saved is not None and saved["status"] == "open"
    assert saved["target"] == original["target"]
    row = harness.service.rows[0]
    assert "Objetivo neto no positivo después de costes" in row["reasons"]
    assert row["costs"]["eligible"] is False
    assert row["tracking_costs"]["eligible"] is True
    assert row["tracking_costs"]["target"] == original["target"]
    assert row["exit_reasons"] == []
    assert [n["kind"] for n in await harness.repository.notifications(original["id"])] == ["start"]


async def test_current_friction_can_invalidate_the_original_target(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deterministic(harness, monkeypatch)
    harness.feed.atr = 0.07
    await harness.service.cycle()
    original = (await harness.repository.opportunities())[0]
    # No operaciones nuevas: el seguimiento no inventa un toque del stop.
    harness.feed.bar_volume = 0

    async def wider_book(symbol: str) -> dict[str, Any]:
        return {
            "asks": [[100.14, 100]],
            "bids": [[99.86, 100]],
            "ts": harness.clock.now().isoformat(),
        }

    monkeypatch.setattr(harness.feed, "book", wider_book)
    harness.advance()
    await harness.service.cycle()
    saved = await harness.repository.opportunity(original["id"])
    assert saved is not None and saved["status"] == "closed"
    assert saved["outcome"] == "filters"
    assert saved["target"] == original["target"]
    assert "Objetivo original neto no positivo después de costes" in saved["reason"]
    assert harness.service.rows[0]["tracking_costs"]["target_net_pct"] < 0


async def test_score_exit_and_rearm_require_entry_condition_to_drop(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deterministic(harness, monkeypatch)
    sent: list[str] = []

    async def sender(text: str) -> None:
        sent.append(text)

    dispatcher = NotificationDispatcher(
        harness.repository, sender, harness.clock.now, send_interval=0
    )
    await harness.service.cycle()
    await dispatcher.flush()
    first = (await harness.repository.opportunities())[0]
    harness.feed.scores["BTC/EUR"] = 40
    harness.advance()
    await harness.service.cycle()
    await dispatcher.flush()
    ended = await harness.repository.opportunity(first["id"])
    assert ended is not None and ended["outcome"] == "score"
    assert ended["status"] == "closed"
    assert ended["estimated_return_pct"] is not None
    assert len(sent) == 2
    assert [notice["kind"] for notice in await harness.repository.notifications(first["id"])] == [
        "start",
        "end",
    ]
    harness.feed.scores["BTC/EUR"] = 80
    harness.advance()
    await harness.service.cycle()
    assert len(await harness.repository.opportunities()) == 1
    harness.feed.scores["BTC/EUR"] = 65
    harness.advance()
    await harness.service.cycle()
    harness.feed.scores["BTC/EUR"] = 80
    harness.advance()
    await harness.service.cycle()
    assert len(await harness.repository.opportunities()) == 2
    assert len(await harness.repository.opportunities(status="open")) == 1


@pytest.mark.parametrize(
    ("attribute", "value", "reason"),
    [
        ("quote_lag_seconds", 61, "Cotización ausente o antigua"),
        ("bar_lag_minutes", 3, "Velas ausentes o antiguas"),
        ("bar_volume", 0, "La última vela no acredita volumen negociado"),
    ],
)
async def test_stale_or_synthetic_data_cannot_open_an_opportunity(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
    value: int,
    reason: str,
) -> None:
    deterministic(harness, monkeypatch)
    setattr(harness.feed, attribute, value)
    await harness.service.cycle()
    assert await harness.repository.opportunities() == []
    assert reason in harness.service.rows[0]["reasons"]
    assert await harness.repository.pending_notifications(harness.clock.now()) == []


@pytest.mark.parametrize("score", [50.0, 80.0])
async def test_no_trade_minute_keeps_open_opportunity_and_contiguous_tracking(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    score: float,
) -> None:
    deterministic(harness, monkeypatch)
    await harness.service.cycle()
    first = (await harness.repository.opportunities())[0]
    harness.feed.scores["BTC/EUR"] = score
    harness.feed.bar_volume = 0
    harness.advance()
    await harness.service.cycle()
    saved = await harness.repository.opportunity(first["id"])
    assert saved is not None and saved["status"] == "open"
    assert saved["tracking_gaps"] is False
    assert saved["last_tracked_at"] == harness.clock.now().isoformat()
    assert saved["high_water"] == first["high_water"]
    assert saved["low_water"] == first["low_water"]
    assert harness.service.rows[0]["state"] == "open"
    assert "La última vela no acredita volumen negociado" in harness.service.rows[0]["reasons"]
    assert [n["kind"] for n in await harness.repository.notifications(first["id"])] == ["start"]

    harness.feed.bar_volume = 10
    harness.advance()
    restarted = harness.restart()
    monkeypatch.setattr(restarted, "_score", harness.feed.score)
    await restarted.cycle()
    saved = await harness.repository.opportunity(first["id"])
    assert saved is not None and saved["status"] == "open"
    assert saved["tracking_gaps"] is False
    assert saved["last_tracked_at"] == harness.clock.now().isoformat()
    assert len(await harness.repository.opportunities()) == 1
    assert [n["kind"] for n in await harness.repository.notifications(first["id"])] == ["start"]


@pytest.mark.parametrize("outcome", ["score", "filters"])
async def test_exit_on_no_trade_minute_keeps_reason_without_a_synthetic_return(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    deterministic(harness, monkeypatch)
    await harness.service.cycle()
    first = (await harness.repository.opportunities())[0]
    harness.feed.bar_volume = 0
    if outcome == "score":
        harness.feed.scores["BTC/EUR"] = 40
    else:
        original_tickers = harness.feed.tickers

        async def excessive_spread() -> dict[str, dict[str, Any]]:
            tickers = await original_tickers()
            tickers["BTC/EUR"]["spread_pct"] = 1.0
            return tickers

        monkeypatch.setattr(harness.feed, "tickers", excessive_spread)
    harness.advance()
    await harness.service.cycle()
    saved = await harness.repository.opportunity(first["id"])
    assert saved is not None and saved["status"] == "closed"
    assert saved["outcome"] == outcome
    assert saved["quality"] == "incomplete"
    assert saved["tracking_gaps"] is False
    assert saved["end_price"] is None
    assert saved["estimated_return_pct"] is None
    assert "sin precio negociado" in saved["reason"]
    assert [n["kind"] for n in await harness.repository.notifications(first["id"])] == [
        "start",
        "end",
    ]


async def test_open_candle_cannot_affect_scoring_or_entry_timestamp(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deterministic(harness, monkeypatch)
    harness.feed.include_open_bar = True
    await harness.service.cycle()
    opportunity = (await harness.repository.opportunities())[0]
    assert opportunity["bar_ts"] == harness.clock.now().isoformat()
    stored = harness.store.read("revolutx", "BTC/EUR", "1m", venue="revolutx")
    assert all(bar.close_time <= harness.clock.now() for bar in stored)


@pytest.mark.parametrize("failure", ["quote", "network", "restart_gap"])
async def test_missing_data_interrupts_without_an_invented_return(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    deterministic(harness, monkeypatch)
    await harness.service.cycle()
    first = (await harness.repository.opportunities())[0]
    if failure == "quote":
        harness.feed.quote_lag_seconds = 61
    elif failure == "network":
        harness.feed.tickers_failed = True
    harness.advance(minutes=3 if failure == "restart_gap" else 1)
    service = harness.restart() if failure == "restart_gap" else harness.service
    monkeypatch.setattr(service, "_score", harness.feed.score)
    await service.cycle()
    interrupted = await harness.repository.opportunity(first["id"])
    assert interrupted is not None
    assert interrupted["status"] == "interrupted"
    assert interrupted["quality"] == "incomplete"
    assert interrupted["end_price"] is None
    assert interrupted["estimated_return_pct"] is None
    assert len(await harness.repository.notifications(first["id"])) == 2


async def test_later_market_failure_keeps_earlier_opportunity_and_start_notice(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deterministic(harness, monkeypatch)
    harness.feed.symbols.append("ETH/EUR")
    harness.feed.failed_symbols.add("ETH/EUR")
    await harness.service.cycle()
    opportunities = await harness.repository.opportunities()
    assert len(opportunities) == 1
    assert opportunities[0]["market"] == "revolutx:BTC/EUR"
    assert len(await harness.repository.notifications(opportunities[0]["id"])) == 1
    assert (await harness.service.status())["state"] == "degraded"
    assert (await harness.repository.load_state())["service"]["cycles"] == 1
    assert (
        next(row for row in harness.service.rows if row["market"] == "revolutx:ETH/EUR")["state"]
        == "unavailable"
    )


async def test_bootstrap_history_only_creates_a_current_notice(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deterministic(harness, monkeypatch)
    await harness.service.cycle()
    opportunities = await harness.repository.opportunities()
    assert len(opportunities) == 1
    assert opportunities[0]["created_at"] == harness.clock.now().isoformat()
    assert len(await harness.repository.observations()) == 1
    notifications = await harness.repository.notifications(opportunities[0]["id"])
    assert len(notifications) == 1
    assert notifications[0]["created_at"] == harness.clock.now().isoformat()
    assert len(harness.store.read("revolutx", "BTC/EUR", "1m", venue="revolutx")) >= 201


async def test_real_score_warms_up_from_closed_bars(harness: Harness) -> None:
    await harness.service.cycle()
    row = harness.service.rows[0]
    assert row["score"] is not None
    assert set(row["points"]) == {"tendencia", "momentum", "macd", "rsi", "volumen", "ruptura"}
    assert row["score"] == sum(row["points"].values())
    assert (await harness.service.status())["warmup"] == {"ready": 1, "total": 1}


async def test_start_cycle_stop_leave_paper_history_unchanged(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deterministic(harness, monkeypatch)

    def snapshot() -> dict[str, list[tuple[Any, ...]]]:
        with sqlite3.connect(harness.settings.db_path) as connection:
            return {
                table: connection.execute(f"SELECT * FROM {table}").fetchall()
                for table in ("signals", "orders", "fills", "equity_snapshots", "audit_log")
            }

    before = snapshot()
    await harness.service.run(asyncio.Event(), max_cycles=1)
    await harness.service.close()
    assert snapshot() == before
    assert before["orders"] == [] and before["fills"] == []
    assert len(before["equity_snapshots"]) == 1
    assert len(await harness.repository.opportunities()) == 1
    assert harness.feed.closed


async def test_overlapping_cycles_do_not_duplicate_requests_or_observations(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deterministic(harness, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = harness.feed.bars
    calls = 0

    async def blocked(symbol: str, since: datetime, now: datetime) -> list[Bar]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return await original(symbol, since, now)

    monkeypatch.setattr(harness.feed, "bars", blocked)
    first = asyncio.create_task(harness.service.cycle())
    await asyncio.wait_for(entered.wait(), timeout=10)
    await harness.service.cycle()
    release.set()
    await first
    assert calls == 1
    assert harness.service.cycles == 1
    assert len(await harness.repository.observations()) == 1


async def test_changed_configuration_interrupts_before_mixing_new_filters(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deterministic(harness, monkeypatch)
    await harness.service.cycle()
    original = (await harness.repository.opportunities())[0]
    harness.advance()
    restarted = AnalysisService(
        AnalysisConfig(entry_score=75),
        harness.feed,
        harness.repository,
        harness.store,
        clock=harness.clock,
    )
    monkeypatch.setattr(restarted, "_score", harness.feed.score)
    await restarted.cycle()
    saved = await harness.repository.opportunities()
    assert len(saved) == 1
    assert saved[0]["status"] == "interrupted"
    assert saved[0]["reason"] == "Configuración de análisis cambiada"
    assert saved[0]["estimated_return_pct"] is None
    assert saved[0]["rule_version"] == original["rule_version"]
    assert saved[0]["config"] == original["config"]
    assert saved[0]["rule_version"] != restarted.rule_version
    assert len(await harness.repository.notifications(original["id"])) == 2


async def test_held_market_becoming_inactive_ends_by_filters(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deterministic(harness, monkeypatch)
    await harness.service.cycle()
    original = (await harness.repository.opportunities())[0]
    original_markets = harness.feed.markets

    async def inactive() -> dict[str, dict[str, Any]]:
        markets = await original_markets()
        markets["BTC/EUR"]["active"] = False
        return markets

    monkeypatch.setattr(harness.feed, "markets", inactive)
    harness.advance()
    restarted = harness.restart()
    monkeypatch.setattr(restarted, "_score", harness.feed.score)
    await restarted.cycle()
    saved = await harness.repository.opportunity(original["id"])
    assert saved is not None and saved["outcome"] == "filters"
    assert saved["status"] == "closed"
    assert "Mercado inactivo" in saved["reason"]
    assert restarted.selected == ["BTC/EUR"]


async def test_stale_candle_cannot_rearm_a_closed_opportunity(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deterministic(harness, monkeypatch)
    await harness.service.cycle()
    harness.feed.scores["BTC/EUR"] = 40
    harness.advance()
    await harness.service.cycle()
    assert len(await harness.repository.opportunities(status="open")) == 0
    harness.feed.scores["BTC/EUR"] = 60
    harness.feed.bar_lag_minutes = 3
    harness.advance(minutes=3)
    await harness.service.cycle()
    assert "Velas ausentes o antiguas" in harness.service.rows[0]["reasons"]
    harness.feed.scores["BTC/EUR"] = 80
    harness.feed.bar_lag_minutes = 0
    harness.advance()
    await harness.service.cycle()
    assert len(await harness.repository.opportunities()) == 1
    assert len(await harness.repository.opportunities(status="open")) == 0


async def test_candle_expiring_during_book_io_cannot_open_an_opportunity(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deterministic(harness, monkeypatch)
    harness.feed.bar_lag_minutes = 2
    original_book = harness.feed.book

    async def delayed_book(symbol: str) -> dict[str, Any]:
        harness.clock.set(harness.clock.now() + timedelta(seconds=1))
        return await original_book(symbol)

    monkeypatch.setattr(harness.feed, "book", delayed_book)
    await harness.service.cycle()
    row = harness.service.rows[0]
    assert "Velas ausentes o antiguas" in row["reasons"]
    assert "Cotización ausente o antigua" not in row["reasons"]
    assert row["costs"] is not None and row["costs"]["eligible"]
    assert await harness.repository.opportunities() == []


async def test_old_unselected_opportunity_recovers_bounded_horizons_without_late_prices(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deterministic(harness, monkeypatch)
    await harness.service.cycle()
    original = (await harness.repository.opportunities())[0]
    created = datetime.fromisoformat(original["created_at"])
    harness.feed.scores["BTC/EUR"] = 40
    harness.advance()
    await harness.service.cycle()
    harness.clock.set(created + timedelta(hours=12))
    harness.feed.symbols = ["ETH/EUR"]
    harness.feed.scores["ETH/EUR"] = 0
    original_bars = harness.feed.bars
    recovered_windows: list[tuple[datetime, datetime]] = []

    async def history(symbol: str, since: datetime, now: datetime) -> list[Bar]:
        rows = await original_bars(symbol, since, now)
        if symbol == "BTC/EUR":
            recovered_windows.append((since, now))
            # A faulty provider may return a later price despite the requested window.
            rows.append(
                Bar(
                    "revolutx",
                    symbol,
                    "1m",
                    harness.clock.now() - timedelta(minutes=1),
                    open=900,
                    high=999,
                    low=800,
                    close=950,
                    volume=10,
                )
            )
        return rows

    monkeypatch.setattr(harness.feed, "bars", history)
    restarted = harness.restart()
    monkeypatch.setattr(restarted, "_score", harness.feed.score)
    await restarted.cycle()
    saved = await harness.repository.opportunity(original["id"])
    assert saved is not None and saved["status"] == "closed"
    assert restarted.selected == ["ETH/EUR"]
    assert len(recovered_windows) == 1
    start, end = recovered_windows[0]
    assert start == created
    assert end <= created + timedelta(hours=4, minutes=1)
    for label, minutes in (("15m", 15), ("1h", 60), ("4h", 240)):
        horizon = saved["horizons"][label]
        assert horizon["quality"] == "complete"
        assert horizon["at"] == (created + timedelta(minutes=minutes)).isoformat()
        assert horizon["return_pct"] == pytest.approx((100 / original["entry"] - 1) * 100)
    assert saved["high_water"] == 100.5
    assert len(await harness.repository.notifications(original["id"])) == 2
    stored = harness.store.read("revolutx", "BTC/EUR", "1m", venue="revolutx")
    assert max(bar.close_time for bar in stored) <= end
    assert max(bar.high for bar in stored) < 200


async def test_ranking_stays_complete_until_every_market_in_next_cycle_finishes(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deterministic(harness, monkeypatch)
    harness.feed.symbols.append("ETH/EUR")
    await harness.service.cycle()
    previous = copy.deepcopy(harness.service.rows)
    assert len(previous) == 2
    harness.advance()
    harness.feed.scores.update({"BTC/EUR": 90, "ETH/EUR": 30})
    entered, release = asyncio.Event(), asyncio.Event()
    original_bars = harness.feed.bars

    async def block_second(symbol: str, since: datetime, now: datetime) -> list[Bar]:
        if symbol == "ETH/EUR":
            entered.set()
            await release.wait()
        return await original_bars(symbol, since, now)

    monkeypatch.setattr(harness.feed, "bars", block_second)
    pending = asyncio.create_task(harness.service.cycle())
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        assert harness.service.rows == previous
    finally:
        release.set()
        await pending
    assert len(harness.service.rows) == 2
    assert [row["score"] for row in harness.service.rows] == [90, 30]
