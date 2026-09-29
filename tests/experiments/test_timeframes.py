from __future__ import annotations

import copy
import json
import math
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from invertio.analysis.market import AnalysisFeed
from invertio.api.app import create_app
from invertio.api.context import ApiContext
from invertio.config.settings import Settings
from invertio.core.clock import SimClock
from invertio.core.models import Bar
from invertio.data.store import BarStore
from invertio.experiments import policies
from invertio.experiments import timeframe_service as service_module
from invertio.experiments.service import KEY as ORIGINAL_KEY
from invertio.experiments.service import ExperimentsService
from invertio.experiments.timeframe_policies import aggregate, definitions, evaluate
from invertio.experiments.timeframe_service import KEY, TimeframeExperimentsService
from invertio.persistence.db import create_engine_async, session_factory, upgrade_db
from invertio.simulation.config import SimulationConfig
from invertio.simulation.repository import SimulationRepository
from tests.experiments.test_service import inputs
from tests.helpers import repo_config

START = datetime(2026, 9, 1, tzinfo=UTC)
BASES = ("score_base", "trend_ema", "breakout", "rsi_rebound")


def series(timeframe: str, prices: list[float], start: datetime = START) -> list[Bar]:
    step = {"1m": 1, "5m": 5, "10m": 10, "1h": 60}[timeframe]
    return [
        Bar(
            "revolutx",
            "BTC/EUR",
            timeframe,
            start + timedelta(minutes=index * step),
            price,
            price * 1.002,
            price * 0.998,
            price * (1.001 if index % 3 else 0.999),
            0 if index % 7 == 0 else 10 + index % 5,
        )
        for index, price in enumerate(prices)
    ]


def wave(count: int) -> list[float]:
    # Tendencia, caída y recuperación: provoca cruces, rupturas y rebotes de RSI.
    return [100 + 8 * math.sin(index / 9) + index * 0.03 for index in range(count)]


def minute_at(close: datetime) -> list[Bar]:
    return [Bar("revolutx", "BTC/EUR", "1m", close - timedelta(minutes=1), 1, 1, 1, 1, 1)]


def test_definitions_reuse_the_four_rules_with_lifetime_in_candles() -> None:
    values = definitions()
    assert [value["id"] for value in values] == [
        *(f"{base}_10m" for base in BASES),
        *(f"{base}_1h" for base in BASES),
    ]
    base = {value["id"]: value for value in policies.definitions()}
    for value in values:
        original = base[value["base_strategy"]]
        config = SimulationConfig.model_validate(value["simulation_config"])
        candles = original["simulation_config"]["lifetime_minutes"]
        assert config.lifetime_minutes == candles * value["timeframe_minutes"]
        assert value["simulation_config"] == {
            **original["simulation_config"],
            "lifetime_minutes": config.lifetime_minutes,
        }
        assert value["version"] == "timeframes-v1"
        assert value["warmup_bars"] == original["warmup_bars"]
        assert value["sources"] == original["sources"]
        # Mismas condiciones de entrada y salida; sólo cambia el texto de la caducidad.
        assert value["rules"][0] == original["rules"][0] or "caducidad" in original["rules"][0]
        assert any(f"({candles} velas)" in rule for rule in value["rules"])
        assert not any("1 minuto" in rule for rule in value["rules"])
    assert "caducidad de 20 horas (120 velas)" in values[3]["rules"][2]
    assert values[4]["rules"][1].endswith("caducidad de 10 días (240 velas).")
    values[0]["simulation_config"]["initial_eur"] = "500"
    assert definitions()[0]["simulation_config"]["initial_eur"] == "50"
    json.dumps(definitions(), allow_nan=False)
    # El grupo de un minuto conserva exactamente sus definiciones guardadas.
    assert {value["version"] for value in policies.definitions()} == {"prospective-v1"}


def test_aggregate_uses_only_complete_utc_blocks() -> None:
    bars = series("5m", [10, 11, 12, 13, 14], START + timedelta(minutes=5))
    joined = aggregate(bars, 10)
    # 00:05 queda sola (su bloque empieza a las 00:00); 00:30 no tiene pareja.
    assert [bar.open_time for bar in joined] == [
        START + timedelta(minutes=10),
        START + timedelta(minutes=20),
    ]
    first = joined[0]
    assert (first.timeframe, first.open, first.close) == ("10m", 11, bars[2].close)
    assert first.high == max(bars[1].high, bars[2].high)
    assert first.low == min(bars[1].low, bars[2].low)
    assert first.volume == bars[1].volume + bars[2].volume
    gap = [bars[1], bars[3]]
    assert aggregate(gap, 10) == []
    hourly = series("1h", [1, 2])
    assert aggregate(hourly, 60) == hourly


@pytest.mark.parametrize("base", BASES)
@pytest.mark.parametrize("timeframe", ["10m", "1h"])
def test_same_rules_as_one_minute_group(base: str, timeframe: str) -> None:
    """Con los mismos precios, la vela larga decide igual que la de un minuto."""
    prices = wave(420)
    minutes = 60 if timeframe == "1h" else 10
    long_bars = series(timeframe, prices)
    one_minute = [
        replace(bar, timeframe="1m", open_time=START + timedelta(minutes=i))
        for i, bar in enumerate(long_bars)
    ]
    native = long_bars
    if timeframe == "10m":
        # Dos velas de 5 minutos que suman exactamente cada vela de 10.
        native = []
        for bar in long_bars:
            middle = (bar.open + bar.close) / 2
            native += [
                Bar(
                    "revolutx",
                    "BTC/EUR",
                    "5m",
                    bar.open_time,
                    bar.open,
                    bar.high,
                    min(bar.low, middle),
                    middle,
                    bar.volume / 2,
                ),
                Bar(
                    "revolutx",
                    "BTC/EUR",
                    "5m",
                    bar.open_time + timedelta(minutes=5),
                    middle,
                    max(middle, bar.close),
                    bar.low,
                    bar.close,
                    bar.volume / 2,
                ),
            ]
        assert [(b.open, b.high, b.low, b.close) for b in aggregate(native, 10)] == [
            (b.open, b.high, b.low, b.close) for b in long_bars
        ]
    decisions = 0
    for end in range(210, 421):
        close = long_bars[end - 1].close_time
        mine = evaluate(
            f"{base}_{timeframe}",
            native[: end * (2 if timeframe == "10m" else 1)],
            minute_at(close),
            close + timedelta(seconds=30),
        )
        reference = policies.evaluate(base, one_minute[:end], one_minute[end - 1].close_time)
        assert mine["ready"] == reference["ready"]
        for key in ("enter", "exit", "entry_reason", "exit_reason"):
            assert mine[key] == reference[key], (end, key)
        assert mine["atr"] == pytest.approx(reference["atr"])
        assert mine["indicators"]["timeframe_minutes"] == minutes
        assert mine["signal_bar_ts"] == close.isoformat()
        assert mine["current_bar_ts"] == close.isoformat()
        decisions += mine["enter"] + mine["exit"]
    assert decisions > 0


def test_freshness_overdue_and_late_entries() -> None:
    prices = wave(300)
    hourly = series("1h", prices)
    close = hourly[-1].close_time
    # Sin velas de un minuto no hay frescura: el servicio lo trata como dato antiguo.
    assert evaluate("trend_ema_1h", hourly, [], close)["current_bar_ts"] is None
    overdue = evaluate(
        "trend_ema_1h",
        hourly,
        minute_at(close + timedelta(minutes=66)),
        close + timedelta(minutes=66),
    )
    assert overdue["ready"] is False
    assert overdue["entry_reason"] == "Falta la vela de 1 hora más reciente"
    within = evaluate(
        "trend_ema_1h",
        hourly,
        minute_at(close + timedelta(minutes=64)),
        close + timedelta(minutes=64),
    )
    assert within["ready"] is True
    short = evaluate("score_base_1h", hourly[:150], [], close)
    assert short["entry_reason"] == "Preparando indicadores: 150/201 velas consecutivas de 1 hora"
    # Encuentra una entrada y compruébala vista tarde.
    for end in range(60, 300):
        at = hourly[end - 1].close_time
        if evaluate("trend_ema_1h", hourly[:end], minute_at(at), at)["enter"]:
            late = evaluate("trend_ema_1h", hourly[:end], minute_at(at), at + timedelta(minutes=6))
            assert late["enter"] is False
            assert late["entry_reason"].startswith("La vela de la señal cerró hace más de 5")
            break
    else:
        pytest.fail("La serie de prueba no produce ninguna entrada")
    with pytest.raises(ValueError):
        evaluate("trend_ema", hourly, [], close)
    with pytest.raises(ValueError):
        evaluate("trend_ema_1h", hourly, [], close.replace(tzinfo=None))


class Feed:
    def __init__(self, clock: SimClock) -> None:
        self.clock = clock
        self.requests: list[tuple[str, str, datetime]] = []
        self.book_calls = 0
        self.fail = False
        self.missing: set[datetime] = set()  # velas que Revolut X no tendría

    async def bars(
        self, symbol: str, since: datetime, now: datetime, *, timeframe: str = "1m"
    ) -> list[Bar]:
        self.requests.append((symbol, timeframe, since))
        if self.fail:
            raise ConnectionError("sin datos")
        step = {"5m": 5, "1h": 60}[timeframe]
        start = since.replace(minute=since.minute - since.minute % step, second=0)
        if start < since:
            start += timedelta(minutes=step)
        result = []
        while start + timedelta(minutes=step) <= now:
            if start not in self.missing:
                result.append(Bar("revolutx", symbol, timeframe, start, 100, 101, 99, 100, 1))
            start += timedelta(minutes=step)
        return result

    async def book(self, symbol: str) -> dict[str, Any]:
        self.book_calls += 1
        return {
            "ts": self.clock.now().isoformat(),
            "bids": [["99.99", "100"]],
            "asks": [["100.01", "100"]],
        }


@pytest.fixture
async def trial(tmp_path: Path) -> AsyncIterator[Any]:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    upgrade_db(settings)
    db = create_engine_async(settings)
    sessions = session_factory(db)
    clock = SimClock(datetime(2026, 9, 29, 10, 3, tzinfo=UTC))
    feed = Feed(clock)
    store = BarStore(tmp_path / "bars")
    service = TimeframeExperimentsService(
        SimulationRepository(sessions, key=KEY), cast(AnalysisFeed, feed), clock=clock, store=store
    )
    await service.start()
    try:
        yield service, feed, clock, store, sessions, settings
    finally:
        await db.dispose()


async def test_long_candles_are_requested_only_when_a_new_one_closes(trial: Any) -> None:
    service, feed, clock, store, *_ = trial
    await service.refresh(["BTC/EUR", "ETH/EUR"])
    # Primer arranque: 1000 velas de cada tipo, una petición por mercado y periodo.
    assert [(s, tf) for s, tf, _ in feed.requests] == [
        ("BTC/EUR", "5m"),
        ("ETH/EUR", "5m"),
        ("BTC/EUR", "1h"),
        ("ETH/EUR", "1h"),
    ]
    assert feed.requests[0][2] == clock.now() - timedelta(minutes=5 * 1000)
    assert len(store.read("revolutx", "BTC/EUR", "1h", venue="revolutx")) == 999
    feed.requests.clear()
    for minute in (4, 9):  # 10:04 y 10:09: aún no ha cerrado otro bloque de 10 minutos
        clock.set(clock.now().replace(minute=minute))
        await service.refresh(["BTC/EUR"])
    assert feed.requests == []
    clock.set(clock.now().replace(minute=10, second=20))
    await service.refresh(["BTC/EUR"])
    assert [(s, tf) for s, tf, _ in feed.requests] == [("BTC/EUR", "5m")]
    assert service._native[("BTC/EUR", "5m")][-1].close_time == clock.now().replace(second=0)
    feed.requests.clear()
    clock.set(clock.now().replace(hour=11, minute=0, second=30))
    await service.refresh(["BTC/EUR"])
    assert sorted(tf for _, tf, _ in feed.requests) == ["1h", "5m"]
    # Tras reiniciar, lo guardado en disco evita volver a descargar el histórico.
    restarted = TimeframeExperimentsService(
        service.repository, cast(AnalysisFeed, feed), clock=clock, store=store
    )
    feed.requests.clear()
    await restarted.refresh(["BTC/EUR"])
    assert feed.requests == []
    feed.fail = True
    clock.set(clock.now() + timedelta(minutes=10))
    await restarted.refresh(["BTC/EUR"])
    assert restarted.data_errors == {"BTC/EUR 5m": "ConnectionError"}


async def test_cycle_trades_with_long_signals_and_keeps_other_groups_apart(
    trial: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, feed, clock, _, sessions, _ = trial
    original = ExperimentsService(
        SimulationRepository(sessions, key=ORIGINAL_KEY), cast(AnalysisFeed, feed), clock=clock
    )
    await original.start()
    original_state = copy.deepcopy(original.state)
    seen: list[tuple[str, int]] = []
    decision = {"enter": True, "exit": False}

    def fake(ident: str, native: list[Bar], minute: list[Bar], now: datetime) -> dict[str, Any]:
        seen.append((ident, len(native)))
        return {
            **decision,
            "ready": True,
            "atr": 1,
            "signal_bar_ts": now.replace(second=0).isoformat(),
            "current_bar_ts": now.isoformat(),
            "entry_reason": "Test",
            "exit_reason": "Test",
            "indicators": {},
        }

    monkeypatch.setattr(service_module, "evaluate", fake)
    assert len(service.state["portfolios"]) == 8
    await service.cycle(**inputs(clock.now()), analysis_cycle=1)
    assert service.last_error is None
    # En el primer ciclo aún no había velas largas; se descargan después de decidir.
    assert {count for _, count in seen} == {0}
    assert [tf for _, tf, _ in feed.requests] == ["5m", "1h"]
    for portfolio in service.state["portfolios"].values():
        assert set(portfolio["positions"]) == {"BTC/EUR"}
    assert service.holding_symbols() == ["BTC/EUR"]
    seen.clear()
    clock.set(clock.now() + timedelta(minutes=1))
    await service.cycle(**inputs(clock.now()), analysis_cycle=2)
    assert {count for ident, count in seen if ident.endswith("_1h")} == {999}
    # La misma señal no se repite en el mismo ciclo del análisis.
    requests = len(feed.requests)
    await service.cycle(**inputs(clock.now()), analysis_cycle=2)
    assert len(feed.requests) == requests
    decision.update(enter=False, exit=True)
    clock.set(clock.now() + timedelta(minutes=1))
    await service.cycle(**inputs(clock.now()), analysis_cycle=3)
    for portfolio in service.state["portfolios"].values():
        assert not portfolio["positions"]
        assert portfolio["trades"][-1]["symbol"] == "BTC/EUR"
    status = service.status(True)
    assert status["name"] == "Velas de 10 minutos y 1 hora"
    assert status["history_ready"] == {"5m": 1, "1h": 1}
    assert original.state == original_state


async def test_saved_group_refuses_changed_rules(trial: Any) -> None:
    service, feed, clock, store, *_ = trial
    saved = copy.deepcopy(service.state)
    saved["definitions"][0]["simulation_config"]["lifetime_minutes"] = 1
    await service.repository.save(saved)
    restarted = TimeframeExperimentsService(
        service.repository, cast(AnalysisFeed, feed), clock=clock, store=store
    )
    with pytest.raises(ValueError, match="otra versión de reglas"):
        await restarted.start()


async def test_status_route_reads_the_saved_group(tmp_path: Path, trial: Any) -> None:
    service, _, _, _, sessions, settings = trial
    ctx = ApiContext(settings, repo_config(), None, sessions, BarStore(tmp_path / "bars"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(ctx)), base_url="http://localhost"
    ) as client:
        saved = (await client.get("/api/timeframe-experiment/status")).json()
        assert saved["available"] is True and saved["running"] is False
        assert len(saved["strategies"]) == 8
        ctx.timeframe_experiment = service
        live = (await client.get("/api/timeframe-experiment/status")).json()
        assert live["history_ready"] == {"5m": 0, "1h": 0}
        exported = await client.get("/api/timeframe-experiment/export.csv")
        assert exported.status_code == 200
        assert "estrategias-10min-1h.csv" in exported.headers["content-disposition"]


async def test_long_candles_continue_from_one_minute_bars_without_requests(trial: Any) -> None:
    service, feed, clock, *_ = trial
    await service.refresh(["BTC/EUR"])
    feed.requests.clear()
    last_5m = service._native[("BTC/EUR", "5m")][-1].open_time  # 09:55
    last_1h = service._native[("BTC/EUR", "1h")][-1].open_time  # 09:00
    clock.set(datetime(2026, 9, 29, 11, 0, 40, tzinfo=UTC))
    minute = [
        Bar(
            "revolutx",
            "BTC/EUR",
            "1m",
            last_1h + timedelta(hours=1, minutes=index),
            100 + index,
            101 + index,
            99,
            100.5 + index,
            2,
        )
        for index in range(60)
        if index != 32  # hueco en 10:32: el bloque 10:30-10:35 no se puede unir
    ]
    service.extend({"BTC/EUR": minute})
    five = service._native[("BTC/EUR", "5m")]
    assert five[-1].open_time == datetime(2026, 9, 29, 10, 25, tzinfo=UTC)
    assert all(b.open_time - a.open_time == timedelta(minutes=5) for a, b in pairwise(five))
    assert len([bar for bar in five if bar.open_time > last_5m]) == 6  # 10:00 … 10:25
    block = five[-1]
    assert (block.open, block.high, block.low, block.close, block.volume) == (
        125,
        130,
        99,
        129.5,
        10,
    )
    # La vela de 10:00-11:00 tiene el hueco de 10:32: no se une, se pide la nativa.
    assert service._native[("BTC/EUR", "1h")][-1].open_time == last_1h
    await service.refresh(["BTC/EUR"])
    assert [tf for _, tf, _ in feed.requests] == ["5m", "1h"]
    # Sin hueco, la hora completa sale de las velas de un minuto y no hay peticiones.
    feed.requests.clear()
    clock.set(datetime(2026, 9, 29, 12, 0, 40, tzinfo=UTC))
    full = [
        Bar(
            "revolutx",
            "BTC/EUR",
            "1m",
            datetime(2026, 9, 29, 11, index, tzinfo=UTC),
            100,
            101,
            99,
            100,
            1,
        )
        for index in range(60)
    ]
    service.extend({"BTC/EUR": full})
    hour = service._native[("BTC/EUR", "1h")][-1]
    assert (hour.open_time, hour.volume, hour.timeframe) == (
        datetime(2026, 9, 29, 11, tzinfo=UTC),
        60,
        "1h",
    )
    await service.refresh(["BTC/EUR"])
    assert feed.requests == []


async def test_download_budget_leaves_the_rest_for_the_next_cycle(trial: Any) -> None:
    service, feed, *_ = trial
    service.refresh_budget_seconds = 0
    await service.refresh(["BTC/EUR", "ETH/EUR"])
    assert feed.requests == []
    service.refresh_budget_seconds = 15
    await service.refresh(["BTC/EUR", "ETH/EUR"])
    assert len(feed.requests) == 4


async def test_restart_decides_in_first_cycle_with_stored_candles(
    trial: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, feed, clock, store, *_ = trial
    await service.refresh(["BTC/EUR"])
    feed.requests.clear()
    seen: list[int] = []

    def fake(ident: str, native: list[Bar], minute: list[Bar], now: datetime) -> dict[str, Any]:
        seen.append(len(native))
        return {
            "ready": False,
            "enter": False,
            "exit": False,
            "entry_reason": "",
            "exit_reason": "",
            "indicators": {},
            "signal_bar_ts": None,
            "current_bar_ts": now.isoformat(),
            "atr": None,
        }

    monkeypatch.setattr(service_module, "evaluate", fake)
    restarted = TimeframeExperimentsService(
        service.repository, cast(AnalysisFeed, feed), clock=clock, store=store
    )
    await restarted.cycle(**inputs(clock.now()), analysis_cycle=1)
    # Sin red y antes de decidir: las posiciones abiertas no se quedan sin datos.
    assert set(seen) == {999}
    assert feed.requests == []


async def test_restart_with_stale_disk_keeps_open_positions(
    trial: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regresión 29/09/2026: tras un reinicio con velas de disco antiguas se vendían todas."""
    service, feed, clock, _, _, _ = trial

    def fake(ident: str, native: list[Bar], minute: list[Bar], now: datetime) -> dict[str, Any]:
        current = bool(native) and now - native[-1].close_time <= timedelta(minutes=70)
        return {"enter": True, "exit": False, "ready": current, "atr": 1,
                "signal_bar_ts": now.replace(second=0, microsecond=0).isoformat(),
                "current_bar_ts": now.isoformat(), "entry_reason": "Test", "exit_reason": "Test",
                "indicators": {}}  # fmt: skip

    monkeypatch.setattr(service_module, "evaluate", fake)
    await service.cycle(**inputs(clock.now()), analysis_cycle=1)  # descarga el histórico
    clock.set(clock.now() + timedelta(minutes=1))
    await service.cycle(**inputs(clock.now()), analysis_cycle=2)  # compra
    assert all(p["positions"] for p in service.state["portfolios"].values())
    # Reinicio rápido, pero con un disco sin velas recientes (otra carpeta vacía).
    clock.set(clock.now() + timedelta(minutes=1))
    feed.requests.clear()
    restarted = TimeframeExperimentsService(
        service.repository, cast(AnalysisFeed, feed), clock=clock,
        store=BarStore(tmp_path / "vacío"),
    )  # fmt: skip
    await restarted.cycle(**inputs(clock.now()), analysis_cycle=3)
    assert restarted.last_error is None
    assert {tf for _, tf, _ in feed.requests} == {"5m", "1h"}  # antes de decidir
    assert restarted.state is not None
    for portfolio in restarted.state["portfolios"].values():
        assert set(portfolio["positions"]) == {"BTC/EUR"}
        assert not any("falta de datos" in d["reason"] for d in portfolio["decisions"])


async def test_candles_built_from_minutes_are_saved(trial: Any) -> None:
    service, _, clock, store, *_ = trial
    await service.refresh(["BTC/EUR"])
    last_hour = service._native[("BTC/EUR", "1h")][-1].open_time
    clock.set(datetime(2026, 9, 29, 11, 0, 40, tzinfo=UTC))
    start = last_hour + timedelta(hours=1)
    minutes = [
        Bar("revolutx", "BTC/EUR", "1m", start + timedelta(minutes=i), 100, 101, 99, 100, 1)
        for i in range(60)
    ]
    snapshot = inputs(clock.now())
    snapshot["bars"] = {"BTC/EUR": minutes}
    await service.cycle(**snapshot, analysis_cycle=1)
    saved = store.read("revolutx", "BTC/EUR", "1h", venue="revolutx")
    assert saved[-1].open_time == last_hour + timedelta(hours=1)


async def test_holes_inside_the_series_are_filled_once(trial: Any) -> None:
    """Regresión 29/09/2026: el disco guardaba solo una de cada dos velas de 5 minutos."""
    service, feed, _, store, *_ = trial
    await service.refresh(["BTC/EUR"])
    series = service._native[("BTC/EUR", "5m")]
    hole = series[-10].open_time
    service._native[("BTC/EUR", "5m")] = [bar for bar in series if bar.open_time != hole]
    feed.requests.clear()
    await service.refresh(["BTC/EUR"])
    assert [(tf, since) for _, tf, since in feed.requests] == [("5m", hole)]
    repaired = service._native[("BTC/EUR", "5m")]
    assert all(b.open_time - a.open_time == timedelta(minutes=5)
               for a, b in pairwise(repaired))  # fmt: skip
    assert hole in {bar.open_time for bar in store.read("revolutx", "BTC/EUR", "5m",
                                                         venue="revolutx")}  # fmt: skip
    # Un hueco que Revolut X tampoco tiene se pide una vez y no se insiste.
    other = repaired[-20].open_time
    feed.missing.add(other)
    service._native[("BTC/EUR", "5m")] = [bar for bar in repaired if bar.open_time != other]
    feed.requests.clear()
    await service.refresh(["BTC/EUR"])
    await service.refresh(["BTC/EUR"])
    assert [(tf, since) for _, tf, since in feed.requests] == [("5m", other)]
