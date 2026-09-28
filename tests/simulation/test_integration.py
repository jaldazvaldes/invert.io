from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from invertio.analysis.config import AnalysisConfig
from invertio.analysis.repository import AnalysisRepository
from invertio.analysis.service import AnalysisService
from invertio.api.app import create_app
from invertio.api.context import ApiContext
from invertio.config.settings import Settings
from invertio.core.clock import SimClock
from invertio.data.store import BarStore
from invertio.persistence.db import create_engine_async, session_factory, upgrade_db
from invertio.simulation.config import SimulationConfig
from invertio.simulation.repository import SimulationRepository
from invertio.simulation.service import SimulationService
from tests.analysis.test_service import FakeAnalysisFeed
from tests.helpers import repo_config


class Feed(FakeAnalysisFeed):
    def __init__(self, clock: SimClock) -> None:
        super().__init__(clock)
        self.book_calls = 0
        self.book_failure = False

    async def markets(self) -> dict[str, dict[str, Any]]:
        markets = await super().markets()
        for market in markets.values():
            market.update(
                precision={"amount": 0.00001, "price": 0.01},
                limits={"amount": {"min": 0.00001, "max": 1000}, "cost": {"min": 1}},
                info={
                    "base_step": "0.00001",
                    "quote_step": "0.01",
                    "min_order_size": "0.00001",
                    "max_order_size": "1000",
                    "min_order_size_quote": "1",
                },
            )
        return markets

    async def book(self, symbol: str) -> dict[str, Any]:
        self.book_calls += 1
        if self.book_failure:
            raise ConnectionError("No book")
        return await super().book(symbol)


@pytest.fixture
async def integrated(tmp_path: Path, t0: datetime) -> AsyncIterator[Any]:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    upgrade_db(settings)
    db = create_engine_async(settings)
    sessions = session_factory(db)
    clock = SimClock(t0)
    feed = Feed(clock)
    simulation = SimulationService(
        SimulationConfig(), SimulationRepository(sessions), feed, clock=clock
    )
    await simulation.start()
    service = AnalysisService(
        AnalysisConfig(),
        feed,
        AnalysisRepository(sessions),
        BarStore(tmp_path / "bars"),
        clock=clock,
        simulation=simulation,
    )
    service._score = feed.score  # type: ignore[method-assign]
    ctx = ApiContext(
        settings,
        repo_config(),
        None,
        sessions,
        service.store,
        analysis=service,
        simulation=simulation,
    )
    yield service, simulation, feed, clock, ctx
    await service.close()
    await db.dispose()


async def test_analysis_opens_simulated_position_without_real_or_classic_orders(
    integrated: Any,
) -> None:
    analysis, simulation, feed, clock, ctx = integrated
    await analysis.cycle()
    assert feed.book_calls == 1  # Reutiliza el libro recién consultado por análisis.
    initial = simulation.status(True)
    assert initial["stats"]["open_positions"] == 1
    assert initial["cash_eur"] != "50"
    assert not initial["trades"]
    # El cierre se simula al libro actual cuando cae la puntuación, no al cierre de otra vela.
    feed.scores["BTC/EUR"] = 30
    clock.set(clock.now() + timedelta(minutes=1))
    await analysis.cycle()
    state = simulation.status(True)
    assert state["stats"]["closed_trades"] == 1
    assert len(state["trades"]) == 1
    assert state["trades"][0]["closed_at"] == clock.now().isoformat()
    assert state["stats"]["open_positions"] == 0
    async with ctx.sessions() as session:
        for table in ("orders", "fills", "signals", "manual_orders", "equity_snapshots"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0


async def test_smaller_current_atr_does_not_close_original_simulated_target(
    integrated: Any,
) -> None:
    analysis, simulation, feed, clock, _ = integrated
    await analysis.cycle()
    original = copy.deepcopy(simulation.engine.state["positions"]["BTC/EUR"])
    # Sin movimiento del libro, el ATR nuevo no cubre una NUEVA entrada.
    # La posición abierta sigue teniendo el objetivo y el coste originales.
    feed.atr = 0.01
    clock.set(clock.now() + timedelta(minutes=1))
    await analysis.cycle()
    row = analysis.rows[0]
    assert "Objetivo neto no positivo después de costes" in row["reasons"]
    assert row["exit_reasons"] == []
    assert analysis._opportunities[original["id"]]["status"] == "open"
    state = simulation.status(True)
    assert state["positions"]["BTC/EUR"] == original
    assert state["stats"]["closed_trades"] == 0
    # La separación no impide una salida posterior por las reglas originales.
    feed.scores["BTC/EUR"] = 30
    clock.set(clock.now() + timedelta(minutes=1))
    await analysis.cycle()
    assert simulation.status(True)["stats"]["closed_trades"] == 1


async def test_restart_cycle_idempotence_and_atomic_failure(integrated: Any) -> None:
    analysis, simulation, feed, clock, _ = integrated
    await analysis.cycle()
    original = copy.deepcopy(simulation.engine.state)
    restarted = SimulationService(SimulationConfig(), simulation.repository, feed, clock=clock)
    await restarted.start()
    await restarted.cycle(
        rows=analysis.rows,
        opportunities=list(analysis._opportunities.values()),
        books={},
        markets=analysis._markets,
        analysis_cycle=analysis.cycles,
    )
    assert restarted.engine.state == original
    original_save = restarted.repository.save

    async def fail_save(state: dict[str, Any]) -> None:
        raise OSError("disk unavailable")

    restarted.repository.save = fail_save
    clock.set(clock.now() + timedelta(minutes=1))
    feed.scores["BTC/EUR"] = 30
    rows = copy.deepcopy(analysis.rows)
    rows[0].update(
        score=30,
        quote_ts=clock.now().isoformat(),
        bar_ts=clock.now().isoformat(),
        observed_at=clock.now().isoformat(),
    )
    await restarted.cycle(
        rows=rows,
        opportunities=list(analysis._opportunities.values()),
        books={},
        markets=analysis._markets,
        analysis_cycle=analysis.cycles + 1,
    )
    assert restarted.last_error is not None
    assert restarted.engine.state == original
    assert await simulation.repository.load() == original
    restarted.repository.save = original_save
    await restarted.cycle(
        rows=rows,
        opportunities=list(analysis._opportunities.values()),
        books={},
        markets=analysis._markets,
        analysis_cycle=analysis.cycles + 1,
    )
    assert restarted.last_error is None
    assert restarted.engine.state["stats"]["closed_trades"] == 1


async def test_missing_book_retains_position_and_recovers_at_current_time(integrated: Any) -> None:
    analysis, simulation, feed, clock, _ = integrated
    await analysis.cycle()
    before = simulation.status(True)["cash_eur"]
    feed.book_failure = True
    clock.set(clock.now() + timedelta(minutes=1))
    await analysis.cycle()
    state = simulation.status(True)
    assert state["cash_eur"] == before
    assert state["stats"]["equity_eur"] is None
    assert state["stats"]["waiting_positions"] == 1
    assert state["trades"] == []
    feed.book_failure = False
    clock.set(clock.now() + timedelta(minutes=1))
    await analysis.cycle()
    recovered = simulation.status(True)
    assert recovered["stats"]["closed_trades"] == 1
    assert recovered["trades"][0]["had_data_gap"] is True
    assert recovered["trades"][0]["closed_at"] == clock.now().isoformat()


async def test_api_read_export_and_stopped_history(integrated: Any) -> None:
    analysis, _simulation, _, clock, ctx = integrated
    await analysis.cycle()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(ctx)), base_url="http://localhost"
    ) as client:
        status = (await client.get("/api/simulation/status")).json()
        assert status["running"] is True
        assert status["stats"]["initial_eur"] == "50"
        assert "seen_opportunity_ids" not in status
        export = await client.get("/api/simulation/export.csv")
        assert export.status_code == 200
        assert "simulacion-ficticia.csv" in export.headers["content-disposition"]
        assert "BTC/EUR" in export.text
        assert (await client.post("/api/simulation/buy", json={})).status_code in (404, 405)
        clock.set(clock.now() + timedelta(minutes=3))
        stale = (await client.get("/api/simulation/status")).json()
        assert stale["valuation_stale"] is True
        assert stale["stats"]["equity_eur"] is None
        ctx.simulation = None
        ctx.analysis = None
        stopped = (await client.get("/api/simulation/status")).json()
        assert stopped["available"] is True and stopped["running"] is False
        assert stopped["positions"]


async def test_later_valid_candidate_is_not_skipped_due_to_book_query_cutoff(
    integrated: Any,
) -> None:
    _analysis, simulation, feed, clock, _ctx = integrated
    market = (await feed.markets())["BTC/EUR"]
    rows, opportunities, markets = [], [], {}
    for index in range(6):
        symbol = f"COIN{index}/EUR"
        ident = f"candidate-{index}"
        markets[symbol] = copy.deepcopy(market)
        markets[symbol]["base"] = f"COIN{index}"
        if index < 5:
            markets[symbol]["info"]["min_order_size_quote"] = "20"
        opportunities.append(
            {
                "id": ident,
                "market": f"revolutx:{symbol}",
                "status": "open",
                "created_at": clock.now().isoformat(),
            }
        )
        rows.append(
            {
                "market": f"revolutx:{symbol}",
                "state": "open",
                "score": 80,
                "reasons": [],
                "opportunity_id": ident,
                "quote_ts": clock.now().isoformat(),
                "bar_ts": clock.now().isoformat(),
                "atr_pct": 1,
                "price": 100,
            }
        )
    await simulation.cycle(
        rows=rows, opportunities=opportunities, books={}, markets=markets, analysis_cycle=1
    )
    assert feed.book_calls == 6
    assert simulation.holding_symbols() == ["COIN5/EUR"]
