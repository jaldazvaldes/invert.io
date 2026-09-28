"""Motor de backtest.

Usa las mismas piezas que el modo en vivo: estrategia, gestor de riesgo, portfolio y bus de
eventos. Solo cambian el reloj (simulado) y el bróker (`SimBroker`).

Secuencia por instante de tiempo (varias velas pueden compartirlo):
1. Reloj a la apertura: el bróker ejecuta las órdenes pendientes y revisa stops/take-profits.
2. Reloj al cierre: se publica `BarClosed` → estrategia → señal → riesgo → orden (pendiente
   hasta la vela siguiente).
3. Se anota la equity.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from itertools import groupby
from typing import Any

from invertio.config.app_config import AppConfig
from invertio.core.bus import EventBus
from invertio.core.clock import SimClock
from invertio.core.events import BarClosed, RiskDecision
from invertio.core.models import Bar, Position
from invertio.execution import OrderRouter
from invertio.execution.sim import SimBroker
from invertio.portfolio import ClosedTrade, Portfolio
from invertio.risk import RiskManager
from invertio.strategies import Strategy
from invertio.strategies.runner import StrategyRunner


@dataclass(slots=True)
class BacktestResult:
    strategy_id: str
    venue: str
    symbol: str
    timeframe: str
    initial_cash: Decimal
    final_equity: Decimal
    equity_curve: list[tuple[datetime, Decimal]]
    trades: list[ClosedTrade]
    open_position: Position | None
    first_price: float
    last_price: float
    signals: int = 0
    approved: int = 0
    rejections: Counter[str] = field(default_factory=Counter)
    orders: int = 0
    unfilled_orders: int = 0


async def run_backtest(
    config: AppConfig,
    strategy: Strategy[Any],
    bars: list[Bar],
    initial_cash: Decimal,
) -> BacktestResult:
    if not bars:
        raise ValueError("No hay velas para el backtest")
    venue_ids = {b.venue for b in bars}
    symbols = {b.symbol for b in bars}
    if len(venue_ids) != 1 or len(symbols) != 1:
        raise ValueError("Cada backtest simula un único venue y símbolo")
    venue = config.venue(bars[0].venue)
    bars = sorted(bars, key=lambda b: b.open_time)

    bus = EventBus(strict=True)
    clock = SimClock(bars[0].open_time)
    portfolio = Portfolio(bus, {venue.id: initial_cash}, {venue.id: venue.quote_currency})
    RiskManager(bus, clock, config, portfolio)
    broker = SimBroker(bus, clock, venue, portfolio)
    OrderRouter(bus, {venue.id: broker})
    StrategyRunner(bus, portfolio, {(venue.id, bars[0].symbol): strategy})

    result = BacktestResult(
        strategy_id=strategy.id,
        venue=venue.id,
        symbol=bars[0].symbol,
        timeframe=bars[0].timeframe,
        initial_cash=initial_cash,
        final_equity=initial_cash,
        equity_curve=[],
        trades=[],
        open_position=None,
        first_price=bars[0].open,
        last_price=bars[-1].close,
    )

    def on_decision(decision: RiskDecision) -> None:
        result.signals += 1
        if decision.approved:
            result.approved += 1
        else:
            result.rejections[decision.reason] += 1

    bus.subscribe(RiskDecision, on_decision)

    for open_time, group in groupby(bars, key=lambda b: b.open_time):
        group_bars = list(group)
        clock.set(open_time)
        for bar in group_bars:
            await broker.process_bar(bar)
        clock.set(group_bars[0].close_time)
        for bar in group_bars:
            await bus.publish(BarClosed(bar))
        result.equity_curve.append((clock.now(), portfolio.equity(venue.id)))

    result.final_equity = portfolio.equity(venue.id)
    result.trades = list(portfolio.closed_trades)
    position = portfolio.position(venue.id, result.symbol)
    result.open_position = position if position.is_open else None
    result.orders = len(broker.orders)
    result.unfilled_orders = sum(1 for o in broker.orders.values() if o.filled_quantity == 0)
    return result
