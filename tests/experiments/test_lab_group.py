from __future__ import annotations

import math
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from invertio.analysis.market import AnalysisFeed
from invertio.api.app import create_app
from invertio.api.context import ApiContext
from invertio.config.settings import Settings
from invertio.core.clock import SimClock
from invertio.core.models import Bar, Position, SignalAction
from invertio.core.timeframes import timeframe_delta
from invertio.data.store import BarStore
from invertio.experiments import lab_policies
from invertio.experiments.lab_policies import VARIANTS, daily_from_4h, definitions, evaluate
from invertio.experiments.lab_service import KEY, LabExperimentsService
from invertio.experiments.maker import MakerSimulationEngine
from invertio.persistence.db import create_engine_async, session_factory, upgrade_db
from invertio.simulation.config import SimulationConfig
from invertio.simulation.repository import SimulationRepository
from invertio.strategies import STRATEGIES
from tests.helpers import repo_config

START = datetime(2026, 1, 1, tzinfo=UTC)
H4 = timedelta(hours=4)


def series(count: int, tf: str = "4h", symbol: str = "SOL/EUR") -> list[Bar]:
    step = timeframe_delta(tf)
    bars = []
    for i in range(count):
        price = 100 + 15 * math.sin(i / 11) + 6 * math.sin(i / 3.7) + i * 0.05
        close = price * (1.004 if i % 4 else 0.995)
        bars.append(
            Bar(
                "revolutx",
                symbol,
                tf,
                START + i * step,
                price,
                max(price, close) * 1.003,
                min(price, close) * 0.997,
                close,
                5 + (i * 7) % 11,
            )
        )
    return bars


def btc_daily(rising: bool, days: int = 260) -> list[Bar]:
    return [
        Bar(
            "revolutx",
            "BTC/EUR",
            "1d",
            START - timedelta(days=days - i),
            close,
            close,
            close,
            close,
            1,
        )
        for i in range(days)
        for close in [100 + i if rising else 1000 - i]
    ]


def minute_at(close: datetime) -> list[Bar]:
    return [Bar("revolutx", "SOL/EUR", "1m", close - timedelta(minutes=1), 1, 1, 1, 1, 1)]


class _Ctx:
    def __init__(self, holding: bool) -> None:
        self.holding = holding

    def position(self, venue: str, symbol: str) -> Position:
        return Position(venue, symbol, quantity=Decimal(int(self.holding)))


def test_definitions_keep_lab_parameters_and_maker_entries() -> None:
    values = {value["id"]: value for value in definitions()}
    assert list(values) == [
        "score_4h",
        "score_4h_btc50",
        "score_4h_btc200",
        "breakout_1h_btc50",
        "breakout_1h_btc200",
    ]
    for ident, value in values.items():
        config = SimulationConfig.model_validate(value["simulation_config"])
        assert float(config.stop_atr) == VARIANTS[ident]["params"]["stop_atr"]
        assert config.target_atr >= 1000 and config.lifetime_minutes >= 365 * 24 * 60
        assert value["execution"] == "maker_entry"
        # La compra pasiva espera dos velas, como en el laboratorio.
        assert value["pending_minutes"] == 2 * value["timeframe_minutes"]
        MakerSimulationEngine(config, None, START, pending_minutes=value["pending_minutes"])
        assert value["lab_params"] == VARIANTS[ident]["params"]
        assert (value["btc_filter"] is None) == (ident == "score_4h")
    assert any("media de 200 días" in rule for rule in values["breakout_1h_btc200"]["rules"])
    assert not any("BTC" in rule for rule in values["score_4h"]["rules"])


def test_daily_candles_join_six_complete_4h_candles() -> None:
    bars = series(6 * 3 + 2)
    days = daily_from_4h(bars)
    assert [day.open_time for day in days] == [START + timedelta(days=i) for i in range(3)]
    assert days[0].open == bars[0].open and days[0].close == bars[5].close
    assert days[0].high == max(bar.high for bar in bars[:6])
    assert daily_from_4h(bars[:5] + bars[6:12]) == [days[1]]  # día con hueco: fuera


@pytest.mark.parametrize("ident", ["score_4h_btc50", "breakout_1h_btc50"])
def test_same_decisions_as_the_lab_strategy(ident: str) -> None:
    variant = VARIANTS[ident]
    bars = series(420, variant["timeframe"])
    cls = STRATEGIES[variant["strategy"]]
    flat = cls(cls.params_model.model_validate(variant["params"]))
    held = cls(cls.params_model.model_validate(variant["params"]))
    daily = btc_daily(rising=True)  # filtro siempre encendido: decide la estrategia
    entries = exits = 0
    for end in range(1, len(bars) + 1):
        bought = flat.on_bar(bars[end - 1], _Ctx(False))
        sold = held.on_bar(bars[end - 1], _Ctx(True))
        if end < 230:
            continue
        close = bars[end - 1].close_time
        mine = evaluate(ident, bars[:end], minute_at(close), daily, close)
        assert mine["ready"] is True
        buy = next((s for s in bought if s.action is SignalAction.BUY), None)
        assert mine["enter"] is (buy is not None), end
        assert mine["exit"] is any(s.action is SignalAction.CLOSE for s in sold), end
        if buy is not None:
            assert buy.stop_loss is not None
            stop = buy.price - variant["params"]["stop_atr"] * mine["atr"]
            assert stop == pytest.approx(buy.stop_loss)
        entries += buy is not None
        exits += mine["exit"]
    assert entries > 0 and exits > 0


def test_btc_filter_late_entries_overdue_candles_and_cache() -> None:
    bars = series(420)
    up, down = btc_daily(rising=True), btc_daily(rising=False)
    for end in range(230, 421):
        close = bars[end - 1].close_time
        if evaluate("score_4h_btc50", bars[:end], minute_at(close), up, close)["enter"]:
            break
    else:
        pytest.fail("La serie de prueba no produce compras")
    blocked = evaluate("score_4h_btc50", bars[:end], minute_at(close), down, close)
    assert blocked["enter"] is False and "BTC" in blocked["entry_reason"]
    assert blocked["indicators"]["btc_filter_on"] is False
    assert evaluate("score_4h_btc50", bars[:end], [], [], close)["enter"] is False  # sin BTC
    late = evaluate("score_4h_btc50", bars[:end], [], up, close + timedelta(minutes=6))
    assert late["enter"] is False and late["current_bar_ts"] is None
    overdue = evaluate("score_4h", bars[:end], [], [], close + H4 + timedelta(minutes=6))
    assert overdue["ready"] is False
    short = evaluate("score_4h", bars[:50], [], [], bars[49].close_time)
    assert short["entry_reason"].startswith("Preparando indicadores: 50/")
    wrong = evaluate("breakout_1h_btc50", bars[:end], [], up, close)
    assert wrong["ready"] is False  # velas de 4 h para una regla de 1 h
    cache: dict[tuple[str, str], tuple[Any, dict[str, Any]]] = {}
    evaluate("score_4h", bars[:end], [], [], close, cache=cache)
    cache[("score_4h", "SOL/EUR")][1]["entry_reason"] = "desde la caché"
    assert evaluate("score_4h", bars[:end], [], [], close, cache=cache)["entry_reason"] == (
        "desde la caché"
    )
    later = evaluate("score_4h", bars[: end + 1], [], [], close + H4, cache=cache)
    assert later["entry_reason"] != "desde la caché"  # otra vela: se recalcula
    with pytest.raises(ValueError):
        evaluate("otra", bars, [], [], close)


class Feed:
    def __init__(self, clock: SimClock) -> None:
        self.clock = clock

    async def bars(
        self, symbol: str, since: datetime, now: datetime, *, timeframe: str = "1m"
    ) -> list[Bar]:
        return []

    async def book(self, symbol: str) -> dict[str, Any]:
        return {
            "ts": self.clock.now().isoformat(),
            "bids": [["99.99", "100"]],
            "asks": [["100.01", "100"]],
        }


@pytest.fixture
async def service(tmp_path: Path) -> AsyncIterator[Any]:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    upgrade_db(settings)
    db = create_engine_async(settings)
    sessions = session_factory(db)
    clock = SimClock(datetime(2026, 9, 29, 12, 0, 30, tzinfo=UTC))
    value = LabExperimentsService(
        SimulationRepository(sessions, key=KEY),
        cast(AnalysisFeed, Feed(clock)),
        clock=clock,
        store=BarStore(tmp_path / "bars"),
    )
    await value.start()
    try:
        yield value, clock, sessions, settings, tmp_path
    finally:
        await db.dispose()


async def test_service_uses_maker_engine_and_btc_daily(
    service: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab, clock, sessions, settings, tmp_path = service
    assert len(lab.state["portfolios"]) == 5
    engines = {d["id"]: lab._engine(d, None, clock.now()) for d in lab.state["definitions"]}
    assert all(isinstance(engine, MakerSimulationEngine) for engine in engines.values())
    assert engines["score_4h"].pending_minutes == 480
    assert engines["breakout_1h_btc50"].pending_minutes == 120
    lab._native[("BTC/EUR", "4h")] = series(6 * 5, symbol="BTC/EUR")
    lab.before_decisions(["BTC/EUR"], clock.now())
    assert len(lab._btc_daily) == 5
    seen: list[tuple[str, str]] = []

    def fake(ident: str, native: list[Bar], *args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.append((ident, native[0].timeframe))
        return {"ready": False}

    monkeypatch.setattr("invertio.experiments.lab_service.evaluate", fake)
    lab._native[("SOL/EUR", "1h")] = series(3, "1h")
    lab._native[("SOL/EUR", "4h")] = series(3)
    lab._policy("breakout_1h_btc50", minute_at(clock.now()), clock.now())
    lab._policy("score_4h", minute_at(clock.now()), clock.now())
    assert seen == [("breakout_1h_btc50", "1h"), ("score_4h", "4h")]
    ctx = ApiContext(settings, repo_config(), None, sessions, BarStore(tmp_path / "bars"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(ctx)), base_url="http://localhost"
    ) as client:
        saved = (await client.get("/api/lab-experiment/status")).json()
        assert saved["available"] is True and len(saved["strategies"]) == 5
        ctx.lab_experiment = lab
        live = (await client.get("/api/lab-experiment/status")).json()
        assert live["history_ready"] == {"4h": 0, "1h": 0}
    assert lab_policies.VERSION == "lab-v1"
