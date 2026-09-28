from __future__ import annotations

import copy
import csv
import io
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import text

from invertio.analysis.market import AnalysisFeed
from invertio.api.app import create_app
from invertio.api.context import ApiContext
from invertio.config.settings import Settings
from invertio.core.clock import SimClock
from invertio.data.store import BarStore
from invertio.experiments.service import KEY, ExperimentsService
from invertio.persistence.db import create_engine_async, session_factory, upgrade_db
from invertio.simulation.repository import SimulationRepository
from tests.helpers import repo_config


class NoNetworkFeed:
    async def book(self, symbol: str) -> dict[str, Any]:
        raise AssertionError(f"Las rutas de consulta no deben consultar el libro: {symbol}")


@pytest.fixture
async def api_state(tmp_path: Path) -> AsyncIterator[Any]:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    upgrade_db(settings)
    database = create_engine_async(settings)
    sessions = session_factory(database)
    ctx = ApiContext(settings, repo_config(), None, sessions, BarStore(tmp_path / "bars"))
    repository = SimulationRepository(sessions, key=KEY)
    service = ExperimentsService(
        repository,
        cast(AnalysisFeed, NoNetworkFeed()),
        clock=SimClock(datetime.now(UTC)),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(ctx)), base_url="http://localhost"
    ) as client:
        yield ctx, repository, service, client
    await database.dispose()


def trade(ident: str, symbol: str, at: str, *, closed: bool) -> dict[str, Any]:
    return {
        "id": ident,
        "symbol": symbol,
        "status": "closed" if closed else "open",
        "opened_at": at,
        "closed_at": at if closed else None,
        "quantity": "0.1",
        "entry": "99.9",
        "exit_price": "99" if closed else None,
        "stop": "97.9",
        "target": "102.9",
        "investment_eur": "10",
        "entry_fee_eur": "0.01",
        "exit_fee_eur": "0.01" if closed else None,
        "pnl_eur": "-0.10" if closed else None,
        "reason": '=HYPERLINK("https://example.invalid")',
        "had_data_gap": False,
        "source_rule_version": "experiment-v1:score_base",
    }


async def test_read_only_api_starts_with_four_independent_fifty_euro_portfolios(
    api_state: Any,
) -> None:
    ctx, repository, service, client = api_state
    legacy_repository = SimulationRepository(ctx.sessions)
    legacy = {"legacy": "historial conservado", "cash_eur": "49.35"}
    await legacy_repository.save(legacy)
    await service.start()
    original = await repository.load()
    response = await client.get("/api/experiments/status")
    assert response.status_code == 200
    status = response.json()
    assert status["available"] is True
    assert status["running"] is False
    assert status["state"] == "stopped"
    assert status["reference"]["equity_eur"] == "50"
    assert {item["id"] for item in status["strategies"]} == {
        "score_base",
        "trend_ema",
        "breakout",
        "rsi_rebound",
    }
    for strategy in status["strategies"]:
        assert strategy["config"]["initial_eur"] == "50"
        assert strategy["stats"]["cash_eur"] == "50"
        assert strategy["stats"]["equity_eur"] == "50"
        assert strategy["stats"]["closed_trades"] == 0
        assert strategy["positions"] == {}
        assert strategy["trades"] == []
        assert strategy["started_at"] == status["started_at"]
    assert await repository.load() == original
    assert await legacy_repository.load() == legacy
    assert ctx.experiments is None


async def test_no_saved_experiment_has_unavailable_status_and_empty_csv(api_state: Any) -> None:
    _, repository, _, client = api_state
    status = (await client.get("/api/experiments/status")).json()
    assert status["available"] is False
    assert status["running"] is False
    assert status["strategies"] == []
    exported = await client.get("/api/experiments/export.csv")
    assert exported.status_code == 200
    assert exported.text.startswith("\ufeffexperiment_id,")
    assert list(csv.DictReader(io.StringIO(exported.text.lstrip("\ufeff")))) == []
    assert "comparacion-estrategias.csv" in exported.headers["content-disposition"]
    assert (await client.get("/api/experiments/export.csv?strategy_id=unknown")).status_code == 404
    assert await repository.load() is None


async def test_csv_all_and_one_strategy_preserve_negative_numbers_and_escape_formula(
    api_state: Any,
) -> None:
    _, repository, service, client = api_state
    await service.start()
    saved = copy.deepcopy(service.state)
    at = service.clock.now().isoformat()
    saved["portfolios"]["score_base"]["trades"] = [trade("closed-one", "BTC/EUR", at, closed=True)]
    saved["portfolios"]["trend_ema"]["positions"] = {
        "ETH/EUR": trade("open-two", "ETH/EUR", at, closed=False)
    }
    await repository.save(saved)
    exported = await client.get("/api/experiments/export.csv")
    rows = list(csv.DictReader(io.StringIO(exported.text.lstrip("\ufeff"))))
    assert len(rows) == 2
    by_id = {row["id"]: row for row in rows}
    assert by_id["closed-one"]["pnl_eur"] == "-0.10"
    assert by_id["closed-one"]["reason"].startswith("'=HYPERLINK")
    assert by_id["closed-one"]["strategy_id"] == "score_base"
    assert by_id["closed-one"]["experiment_id"] == saved["experiment_id"]
    assert by_id["open-two"]["status"] == "open"
    assert by_id["open-two"]["closed_at"] == ""
    assert by_id["open-two"]["initial_eur"] == "50"
    selected = await client.get("/api/experiments/export.csv?strategy_id=trend_ema")
    selected_rows = list(csv.DictReader(io.StringIO(selected.text.lstrip("\ufeff"))))
    assert [row["id"] for row in selected_rows] == ["open-two"]
    empty = await client.get("/api/experiments/export.csv?strategy_id=breakout")
    assert list(csv.DictReader(io.StringIO(empty.text.lstrip("\ufeff")))) == []
    assert (await client.get("/api/experiments/export.csv?strategy_id=unknown")).status_code == 404
    assert await repository.load() == saved


async def test_old_valuation_with_position_is_unknown_and_no_strategy_is_ranked(
    api_state: Any,
) -> None:
    _, repository, service, client = api_state
    await service.start()
    saved = copy.deepcopy(service.state)
    old = (service.clock.now() - timedelta(minutes=3)).isoformat()
    saved["last_cycle_at"] = old
    portfolio = saved["portfolios"]["score_base"]
    portfolio["last_cycle_at"] = old
    portfolio["positions"] = {"BTC/EUR": trade("open-stale", "BTC/EUR", old, closed=False)}
    portfolio["stats"].update(
        cash_eur="40",
        equity_eur="50.50",
        unrealized_pnl_eur="0.50",
        return_pct="1",
        open_positions=1,
    )
    await repository.save(saved)
    response = await client.get("/api/experiments/status")
    assert response.status_code == 200
    status = response.json()
    assert status["valuation_stale"] is True
    assert status["running"] is False
    by_id = {item["id"]: item for item in status["strategies"]}
    pending = by_id["score_base"]
    for field in ("equity_eur", "unrealized_pnl_eur", "return_pct", "net_pnl_eur"):
        assert pending["stats"][field] is None
    assert pending["stats"]["cash_eur"] == "40"
    assert pending["positions"]["BTC/EUR"]["investment_eur"] == "10"
    assert by_id["trend_ema"]["stats"]["equity_eur"] == "50"
    assert all(strategy["rank"] is None for strategy in status["strategies"])
    assert await repository.load() == saved


async def test_experiment_endpoints_cannot_submit_orders_and_leave_trade_tables_untouched(
    api_state: Any,
) -> None:
    ctx, _, service, client = api_state
    await service.start()
    app = create_app(ctx)
    experiment_routes = [route for route in app.routes if route.path.startswith("/api/experiments")]
    assert len(experiment_routes) == 2
    assert all(route.methods == {"GET"} for route in experiment_routes)
    assert (await client.get("/api/experiments/status")).status_code == 200
    assert (await client.get("/api/experiments/export.csv")).status_code == 200
    assert (await client.post("/api/experiments/status", json={})).status_code == 405
    assert (await client.post("/api/experiments/export.csv", json={})).status_code == 405
    assert (await client.post("/api/experiments/buy", json={})).status_code in (404, 405)
    assert (await client.post("/api/experiments/sell", json={})).status_code in (404, 405)
    async with ctx.sessions() as session:
        for table in ("orders", "fills", "signals", "manual_orders", "equity_snapshots"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
