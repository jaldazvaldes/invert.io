from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from invertio.config import Settings
from invertio.config.live_config import LiveConfig, LiveMarket
from invertio.core.clock import SimClock
from invertio.core.events import EngineState
from invertio.core.models import Bar, Signal, SignalAction, TradingMode
from invertio.live.engine import LiveEngine, MarketRuntime
from invertio.notify import Button
from invertio.persistence.db import create_engine_async, session_factory, upgrade_db
from invertio.persistence.models import EquitySnapshotRow, FillRow, OrderRow, SignalRow
from invertio.strategies import Strategy, StrategyContext, StrategyParams
from tests.helpers import make_bars, repo_config

D = Decimal
TF = timedelta(minutes=5)


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str, buttons: list[Button] | None = None) -> None:
        self.messages.append(text)

    def find(self, fragment: str) -> list[str]:
        return [m for m in self.messages if fragment in m]


class FakeFeed:
    """Devuelve las velas ya cerradas según el reloj."""

    has_live_spread = True

    def __init__(self, venue: str, bars: list[Bar]) -> None:
        self.venue = venue
        self.bars = bars
        self.offline = False

    async def closed_bars(
        self, symbol: str, timeframe: str, since: datetime, now: datetime
    ) -> list[Bar]:
        if self.offline:
            return []
        return [
            b
            for b in self.bars
            if b.symbol == symbol and b.open_time >= since and b.close_time <= now
        ]

    async def spread_pct(self, symbol: str) -> float | None:
        return 0.01

    async def close(self) -> None:
        pass


class AtTimes(Strategy[StrategyParams]):
    """Compra en la vela que abre en `buy_at` y cierra en `close_at`."""

    id = "at_times"
    params_model = StrategyParams

    def __init__(self, buy_at: datetime, close_at: datetime | None = None) -> None:
        super().__init__(StrategyParams())
        self.buy_at, self.close_at = buy_at, close_at

    @property
    def warmup_bars(self) -> int:
        return 5

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[Signal]:
        if bar.open_time == self.buy_at:
            return [
                Signal(self.id, bar.venue, bar.symbol, SignalAction.BUY, bar.close_time,
                       bar.close, stop_loss=bar.close * 0.97, take_profit=bar.close * 1.05,
                       reason="prueba")
            ]  # fmt: skip
        if bar.open_time == self.close_at:
            return [
                Signal(self.id, bar.venue, bar.symbol, SignalAction.CLOSE, bar.close_time,
                       bar.close, reason="prueba")
            ]  # fmt: skip
        return []


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    upgrade_db(settings)
    engine = create_engine_async(settings)
    yield session_factory(engine)
    await engine.dispose()


def _live_config() -> LiveConfig:
    return LiveConfig(
        initial_cash={"revolutx": D(100)},
        markets=[LiveMarket(market="revolutx:BTC/EUR", strategy="at_times")],
    )


def _engine(
    sessions: async_sessionmaker[AsyncSession],
    clock: SimClock,
    feed: FakeFeed,
    strategy: Strategy[StrategyParams],
    notifier: RecordingNotifier,
) -> LiveEngine:
    config = repo_config(prefer_post_only=False)  # a mercado: precios de ejecución exactos
    return LiveEngine(
        config=config,
        live=_live_config(),
        mode=TradingMode.PAPER,
        timeframe="5m",
        markets=[MarketRuntime(config.venue("revolutx"), "BTC/EUR", strategy, feed)],
        sessions=sessions,
        notifier=notifier,
        clock=clock,
    )


def _after_close(t0: datetime, bars_closed: int) -> datetime:
    return t0 + bars_closed * TF + timedelta(seconds=4)


async def _advance(engine: LiveEngine, clock: SimClock, t0: datetime, bars_closed: int) -> None:
    clock.set(_after_close(t0, bars_closed))
    await engine.tick()


async def test_signal_to_stop_loss_is_notified_and_recorded(
    t0: datetime, sessions: async_sessionmaker[AsyncSession]
) -> None:
    closes = [100.0] * 12 + [101, 102, 95, 94, 94, 94]
    bars = make_bars(closes, t0)
    notifier = RecordingNotifier()
    clock = SimClock(_after_close(t0, 10))
    engine = _engine(
        sessions, clock, FakeFeed("revolutx", bars), AtTimes(bars[12].open_time), notifier
    )

    await engine.start()
    assert notifier.find("en marcha")
    assert not notifier.find("Señal")  # el calentamiento no genera señales

    await _advance(engine, clock, t0, 11)
    await _advance(engine, clock, t0, 12)
    await _advance(engine, clock, t0, 13)  # cierra la vela 12 → señal de compra
    [signal_msg] = notifier.find("Señal de compra")
    assert "Stop" in signal_msg and "Objetivo" in signal_msg

    await _advance(engine, clock, t0, 14)  # la vela 13 ejecuta la compra a su apertura
    [fill_msg] = notifier.find("Compra ejecutada")
    assert "101,00" in fill_msg or "101,0" in fill_msg

    await _advance(engine, clock, t0, 15)  # la vela 14 cae a 95: salta el stop (~97,97)
    [exit_msg] = notifier.find("Venta ejecutada")
    assert "stop-loss" in exit_msg and "−" in exit_msg
    assert not engine.portfolio.open_positions()

    await engine.recorder.flush()
    async with sessions() as session:
        assert (await session.scalar(select(SignalRow.approved))) is True
        orders = (await session.scalars(select(OrderRow))).all()
        assert {o.reason for o in orders} == {"entrada", "stop_loss"}
        assert all(o.status == "filled" for o in orders)
        assert len((await session.scalars(select(FillRow))).all()) == 2
        snapshots = (await session.scalars(select(EquitySnapshotRow))).all()
        assert len(snapshots) >= 5
    await engine.shutdown()


async def test_restart_restores_position_and_its_stop(
    t0: datetime, sessions: async_sessionmaker[AsyncSession]
) -> None:
    closes = [100.0] * 12 + [101, 102, 103, 103, 90, 90]
    bars = make_bars(closes, t0)
    feed = FakeFeed("revolutx", bars)
    clock = SimClock(_after_close(t0, 10))
    first = _engine(sessions, clock, feed, AtTimes(bars[12].open_time), RecordingNotifier())
    await first.start()
    for n in (11, 12, 13, 14):
        await _advance(first, clock, t0, n)
    assert first.portfolio.position("revolutx", "BTC/EUR").is_open
    await first.shutdown()

    # Reinicio mientras la app estaba parada la vela 16 cayó a 90 (por debajo del stop).
    clock.set(_after_close(t0, 17))
    notifier = RecordingNotifier()
    second = _engine(sessions, clock, feed, AtTimes(bars[12].open_time), notifier)
    await second.start()
    assert "posición abierta: BTC/EUR" in notifier.messages[-1] or notifier.find("stop-loss")
    assert not second.portfolio.open_positions()  # el stop se aplicó en la recuperación
    assert notifier.find("stop-loss")
    await second.shutdown()


async def test_warm_up_does_not_record_history_and_restarts_do_not_duplicate(
    t0: datetime, sessions: async_sessionmaker[AsyncSession]
) -> None:
    bars = make_bars([100.0] * 20, t0)
    feed = FakeFeed("revolutx", bars)
    clock = SimClock(_after_close(t0, 10))
    first = _engine(sessions, clock, feed, AtTimes(t0 - TF), RecordingNotifier())
    await first.start()
    await first.recorder.flush()
    await _advance(first, clock, t0, 11)
    await first.shutdown()
    second = _engine(sessions, clock, feed, AtTimes(t0 - TF), RecordingNotifier())
    await second.start()  # mismo instante: nada nuevo que anotar
    await second.shutdown()
    async with sessions() as session:
        stamps = [s.ts for s in (await session.scalars(select(EquitySnapshotRow))).all()]
    # una marca al arrancar (vela 9) + una por la vela 10 en vivo; sin duplicados
    assert stamps == [bars[9].close_time, bars[10].close_time]


async def test_pending_orders_are_canceled_on_restart(
    t0: datetime, sessions: async_sessionmaker[AsyncSession]
) -> None:
    bars = make_bars([100.0] * 16, t0)
    feed = FakeFeed("revolutx", bars)
    clock = SimClock(_after_close(t0, 10))
    first = _engine(sessions, clock, feed, AtTimes(bars[10].open_time), RecordingNotifier())
    await first.start()
    await _advance(first, clock, t0, 11)  # señal: orden pendiente hasta la próxima vela
    assert first.brokers["revolutx"].open_orders()
    await first.shutdown()

    notifier = RecordingNotifier()
    second = _engine(sessions, clock, feed, AtTimes(bars[10].open_time), notifier)
    await second.start()
    assert "1 órdenes pendientes canceladas" in notifier.messages[-1]
    assert not second.portfolio.open_positions()
    await second.shutdown()


async def test_late_bars_do_not_trade(
    t0: datetime, sessions: async_sessionmaker[AsyncSession]
) -> None:
    bars = make_bars([100.0] * 20, t0)
    notifier = RecordingNotifier()
    clock = SimClock(_after_close(t0, 10))
    engine = _engine(
        sessions, clock, FakeFeed("revolutx", bars), AtTimes(bars[12].open_time), notifier
    )
    await engine.start()
    await _advance(engine, clock, t0, 16)  # la vela 12 llega 20 minutos tarde
    assert not notifier.find("Señal")
    await engine.shutdown()


async def test_stale_data_warning_and_recovery(
    t0: datetime, sessions: async_sessionmaker[AsyncSession]
) -> None:
    bars = make_bars([100.0] * 30, t0)
    feed = FakeFeed("revolutx", bars)
    notifier = RecordingNotifier()
    clock = SimClock(_after_close(t0, 10))
    engine = _engine(sessions, clock, feed, AtTimes(t0 - TF), notifier)
    await engine.start()
    feed.offline = True
    for n in (11, 12):
        await _advance(engine, clock, t0, n)
    assert not notifier.find("Sin datos")
    await _advance(engine, clock, t0, 13)  # faltan las velas 10, 11 y 12: 3 velas sin datos
    assert len(notifier.find("Sin datos nuevos")) == 1
    await _advance(engine, clock, t0, 14)
    assert len(notifier.find("Sin datos nuevos")) == 1  # se avisa una sola vez
    feed.offline = False
    await _advance(engine, clock, t0, 15)
    assert notifier.find("recuperados")
    await engine.shutdown()


async def test_panic_closes_everything_and_blocks_entries(
    t0: datetime, sessions: async_sessionmaker[AsyncSession]
) -> None:
    bars = make_bars([100.0] * 20, t0)
    notifier = RecordingNotifier()
    clock = SimClock(_after_close(t0, 10))
    engine = _engine(
        sessions, clock, FakeFeed("revolutx", bars), AtTimes(bars[10].open_time), notifier
    )
    await engine.start()
    await _advance(engine, clock, t0, 11)
    await _advance(engine, clock, t0, 12)
    assert engine.portfolio.open_positions()

    closed = await engine.controller.panic("prueba")
    assert closed == 1
    assert not engine.portfolio.open_positions()
    state_after_panic = engine.controller.state
    assert state_after_panic is EngineState.HALTED
    assert notifier.find("DETENIDO")
    assert engine.portfolio.closed_trades[-1].exit_reason == "pánico"

    await engine.controller.resume("prueba")
    assert engine.controller.state is EngineState.RUNNING
    await engine.shutdown()
