from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from invertio.core.models import Bar
from invertio.experiments import service as experiments_module
from invertio.experiments.service import KEY, ExperimentsService
from invertio.simulation.repository import SimulationRepository
from tests.simulation.test_integration import integrated as integrated


async def test_analysis_shares_feed_with_legacy_and_all_four_experiments(
    integrated: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis, legacy, feed, clock, ctx = integrated
    comparison = ExperimentsService(SimulationRepository(ctx.sessions, key=KEY), feed, clock=clock)
    await comparison.start()
    analysis.experiments = comparison
    exiting = False

    def policy(ident: str, bars: list[Bar], now: datetime) -> dict[str, Any]:
        return {
            "ready": True,
            "enter": not exiting,
            "exit": exiting,
            "bar_ts": bars[-1].close_time.isoformat(),
            "atr": 1.0,
            "entry_reason": "Entrada de la regla de prueba",
            "exit_reason": "Salida de la regla de prueba",
            "indicators": {},
        }

    monkeypatch.setattr(experiments_module, "evaluate", policy)
    await analysis.cycle()
    assert comparison.last_error is None
    assert feed.book_calls == 1
    assert legacy.status(True)["stats"]["open_positions"] == 1
    for row in comparison.status(True)["strategies"]:
        assert row["stats"]["initial_eur"] == "50"
        assert row["stats"]["open_positions"] == 1
        assert row["positions"]["BTC/EUR"]["reason"] == "Entrada de la regla de prueba"
    exiting = True
    feed.scores["BTC/EUR"] = 30
    clock.set(clock.now() + timedelta(minutes=1))
    await analysis.cycle()
    assert comparison.last_error is None
    assert feed.book_calls == 2
    assert legacy.status(True)["stats"]["closed_trades"] == 1
    for row in comparison.status(True)["strategies"]:
        assert row["stats"]["closed_trades"] == 1
        assert row["trades"][0]["reason"] == "Salida de la regla de prueba"
    async with ctx.sessions() as session:
        for table in ("orders", "fills", "signals", "manual_orders", "equity_snapshots"):
            assert await session.scalar(text(f"SELECT COUNT(*) FROM {table}")) == 0
        assert await session.scalar(text("SELECT COUNT(*) FROM simulation_state")) == 2
