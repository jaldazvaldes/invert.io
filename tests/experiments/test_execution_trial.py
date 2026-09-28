from __future__ import annotations

import copy
import csv
import io
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from invertio.api.app import create_app
from invertio.api.context import ApiContext
from invertio.config.settings import Settings
from invertio.core.clock import SimClock
from invertio.data.store import BarStore
from invertio.experiments import execution_service as module
from invertio.experiments.execution_service import KEY, ExecutionExperimentService
from invertio.experiments.service import KEY as ORIGINAL_KEY
from invertio.experiments.service import ExperimentsService
from invertio.persistence.db import create_engine_async, session_factory, upgrade_db
from invertio.simulation.repository import SimulationRepository
from tests.experiments.test_service import inputs
from tests.helpers import repo_config


class Feed:
    def __init__(self, clock: SimClock) -> None:
        self.clock = clock
        self.bid, self.ask = "99.99", "100.01"
        self.calls = 0

    async def book(self, symbol: str) -> dict[str, Any]:
        self.calls += 1
        return {"ts": self.clock.now().isoformat(), "bids": [[self.bid, "100"]],
                "asks": [[self.ask, "100"]]}


@pytest.fixture
async def trial(
    tmp_path: Path, t0: datetime, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Any]:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    upgrade_db(settings)
    db = create_engine_async(settings)
    sessions = session_factory(db)
    clock = SimClock(t0)
    feed = Feed(clock)
    decision = {"enter": True, "exit": False}

    def evaluate(ident: str, bars: Any, now: datetime) -> dict[str, Any]:
        return {**decision, "ready": True, "atr": 1, "signal_bar_ts": now.isoformat(),
                "current_bar_ts": now.isoformat(), "entry_reason": "Test entrada",
                "exit_reason": "Test salida", "indicators": {}}

    monkeypatch.setattr(module, "evaluate", evaluate)
    original = ExperimentsService(
        SimulationRepository(sessions, key=ORIGINAL_KEY), feed, clock=clock  # type: ignore[arg-type]
    )
    await original.start()
    service = ExecutionExperimentService(
        SimulationRepository(sessions, key=KEY), feed, clock=clock  # type: ignore[arg-type]
    )
    await service.start()
    try:
        yield service, original, feed, clock, decision, sessions, settings
    finally:
        await db.dispose()


def snapshot(now: datetime) -> dict[str, Any]:
    value = inputs(now)
    value["markets"]["BTC/EUR"]["precision"]["price"] = 0.01
    return value


async def test_same_closed_signal_is_frozen_but_current_data_quality_is_not(trial: Any) -> None:
    service, _, _, clock, _, _, _ = trial
    cursor: dict[str, Any] = {}
    policy = {"ready": True, "signal_bar_ts": clock.now().isoformat(),
              "current_bar_ts": clock.now().isoformat(), "exit": False, "enter": False,
              "atr": 1, "indicators": {"rsi14": 54.9999}}
    service._stable_policy(policy, cursor)
    changed = {**policy, "exit": True, "atr": 1.1, "indicators": {"rsi14": 55.0001},
               "current_bar_ts": (clock.now() + timedelta(minutes=1)).isoformat()}
    stable = service._stable_policy(changed, cursor)
    assert stable["exit"] is False
    assert stable["atr"] == 1
    assert stable["current_bar_ts"] == changed["current_bar_ts"]
    interrupted = {**changed, "ready": False, "entry_reason": "Datos interrumpidos"}
    assert service._stable_policy(interrupted, cursor) == interrupted
    changed["signal_bar_ts"] = (clock.now() + timedelta(minutes=5)).isoformat()
    assert service._stable_policy(changed, cursor)["exit"] is True


async def test_trial_complete_cycle_keeps_original_and_real_orders_untouched(trial: Any) -> None:
    service, original, feed, clock, decision, sessions, settings = trial
    original_state = copy.deepcopy(original.state)
    started = service.state["started_at"]
    assert all(p["cash_eur"] == "50" for p in service.state["portfolios"].values())
    await service.cycle(**snapshot(clock.now()), analysis_cycle=1)
    assert service.last_error is None
    assert feed.calls == 1
    for ident, portfolio in service.state["portfolios"].items():
        assert portfolio["started_at"] == started
        if ident.endswith("maker"):
            assert not portfolio["positions"]
            assert portfolio["cash_eur"] == "40"
            assert portfolio["stats"]["equity_eur"] == "50"
            assert set(portfolio["pending_orders"]) == {"BTC/EUR"}
        else:
            assert set(portfolio["positions"]) == {"BTC/EUR"}
    assert service.holding_symbols() == ["BTC/EUR"]

    # A restart recovers pending reservations without buying them at creation time.
    saved = copy.deepcopy(service.state)
    restarted = ExecutionExperimentService(service.repository, feed, clock=clock)
    await restarted.start()
    await restarted.cycle(**snapshot(clock.now()), analysis_cycle=1)
    assert restarted.state == saved
    assert feed.calls == 1

    decision["enter"] = False
    clock.set(clock.now() + timedelta(minutes=1))
    feed.bid, feed.ask = "99.96", "99.98"
    await restarted.cycle(**snapshot(clock.now()), analysis_cycle=2)
    assert restarted.last_error is None
    for ident, portfolio in restarted.state["portfolios"].items():
        assert set(portfolio["positions"]) == {"BTC/EUR"}
        if ident.endswith("maker"):
            assert portfolio["positions"]["BTC/EUR"]["entry_fee_eur"] == "0"
            assert not portfolio["pending_orders"]
            assert portfolio["maker_orders"][-1]["status"] == "filled"

    decision["exit"] = True
    clock.set(clock.now() + timedelta(minutes=1))
    await restarted.cycle(**snapshot(clock.now()), analysis_cycle=3)
    assert restarted.last_error is None
    assert all(len(p["trades"]) == 1 for p in restarted.state["portfolios"].values())
    assert all(float(p["trades"][0]["exit_fee_eur"]) > 0
               for p in restarted.state["portfolios"].values())

    decision["exit"] = False
    clock.set(clock.now() + timedelta(minutes=1))
    await restarted.cycle(**snapshot(clock.now()), analysis_cycle=4)
    decision["enter"] = True
    clock.set(clock.now() + timedelta(minutes=1))
    await restarted.cycle(**snapshot(clock.now()), analysis_cycle=5)
    assert restarted.last_error is None
    for ident, portfolio in restarted.state["portfolios"].items():
        active = set(portfolio["positions"]) | set(portfolio.get("pending_orders", {}))
        assert bool(active) == ident.startswith("rsi_1m")
    assert await original.repository.load() == original_state
    async with sessions() as session:
        for table in ("orders", "fills", "signals", "manual_orders"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0

    ctx = ApiContext(settings, repo_config(), None, sessions, BarStore(settings.data_dir / "bars"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(ctx)), base_url="http://localhost"
    ) as client:
        status = (await client.get("/api/execution-experiment/status")).json()
        assert status["experiment_id"] == restarted.state["experiment_id"]
        assert status["started_at"] == started
        assert status["running"] is False
        assert len(status["strategies"]) == 4
        maker = next(s for s in status["strategies"] if s["id"] == "rsi_1m_maker")
        assert maker["stats"]["reserved_eur"] == "10"
        assert len(maker["maker_orders"]) == 1
        assert maker["stats"]["closed_fees_eur"] == maker["trades"][0]["exit_fee_eur"]
        original_status = (await client.get("/api/experiments/status")).json()
        assert original_status["experiment_id"] == original_state["experiment_id"]
        response = await client.get("/api/execution-experiment/maker-orders.csv")
        orders = list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))
        assert len(orders) == 3
        assert {o["status"] for o in orders} == {"pending", "filled"}
        response = await client.get("/api/execution-experiment/export.csv")
        trades = list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))
        assert len(trades) == 5
        assert {t["execution"] for t in trades} == {"taker", "maker_entry"}
