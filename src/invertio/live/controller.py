"""Control del motor en vivo: pausa, reanudación, pánico y resúmenes de estado."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from invertio.config.app_config import AppConfig
from invertio.core.bus import EventBus
from invertio.core.clock import Clock
from invertio.core.events import EngineState, EngineStateChanged, RiskDecision
from invertio.core.models import TradingMode
from invertio.execution.sim import SimBroker
from invertio.portfolio import Portfolio
from invertio.risk import RiskManager
from invertio.strategies import Strategy
from invertio.strategies.runner import StrategyRunner
from invertio.strategies.score import ScoreResult, ScoreStrategy


@dataclass(frozen=True, slots=True)
class PositionStatus:
    symbol: str
    quantity: Decimal
    avg_price: Decimal
    last_price: Decimal | None
    unrealized: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None


@dataclass(frozen=True, slots=True)
class VenueStatus:
    venue: str
    currency: str
    equity: Decimal
    cash: Decimal
    initial: Decimal
    positions: list[PositionStatus]


@dataclass(frozen=True, slots=True)
class DaySummary:
    trades: int
    wins: int
    pnl_by_currency: dict[str, Decimal]
    signals: int
    approved: int
    rejections: list[tuple[str, int]]


@dataclass(slots=True)
class _DayCounters:
    day: object = None
    signals: int = 0
    approved: int = 0
    rejections: Counter[str] = field(default_factory=Counter)


class EngineController:
    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        config: AppConfig,
        mode: TradingMode,
        portfolio: Portfolio,
        risk: RiskManager,
        brokers: dict[str, SimBroker],
        runner: StrategyRunner | None = None,
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._config = config
        self.mode = mode
        self._portfolio = portfolio
        self._risk = risk
        self._brokers = brokers
        self._runner = runner
        self._strategies: dict[tuple[str, str], Strategy[Any]] = (
            runner.assignments if runner else {}
        )
        self._tz = ZoneInfo(config.risk.day_timezone)
        self._counters = _DayCounters()
        self.last_bar: dict[tuple[str, str], datetime] = {}
        bus.subscribe(RiskDecision, self._on_decision)

    @property
    def state(self) -> EngineState:
        return self._risk.state

    # --- acciones ----------------------------------------------------------------------

    async def pause(self, reason: str) -> None:
        if self.state is EngineState.RUNNING:
            await self._bus.publish(EngineStateChanged(EngineState.PAUSED, reason))

    async def resume(self, reason: str) -> None:
        if self.state is not EngineState.RUNNING:
            await self._bus.publish(EngineStateChanged(EngineState.RUNNING, reason))

    def markets(self) -> list[tuple[tuple[str, str], str, bool]]:
        """(mercado, estrategia, activo) de cada mercado vigilado."""
        disabled = self._runner.disabled if self._runner else set()
        return [(key, s.id, key not in disabled) for key, s in self._strategies.items()]

    def set_market_enabled(self, key: tuple[str, str], enabled: bool) -> None:
        if self._runner is None or key not in self._strategies:
            raise KeyError(f"Mercado no vigilado: {key[0]}:{key[1]}")
        if enabled:
            self._runner.disabled.discard(key)
        else:
            self._runner.disabled.add(key)

    async def panic(self, reason: str) -> int:
        """Detiene el motor, cancela todas las órdenes y cierra todas las posiciones.

        Devuelve cuántas posiciones se han cerrado.
        """
        await self._bus.publish(EngineStateChanged(EngineState.HALTED, reason))
        closed = 0
        for venue, broker in self._brokers.items():
            await broker.cancel_all()
            for position in self._portfolio.open_positions(venue):
                await broker.flatten(position.symbol, reason="pánico")
                closed += 1
        return closed

    # --- consultas ---------------------------------------------------------------------

    def status(self) -> list[VenueStatus]:
        result = []
        for venue in self._config.venues:
            if venue.id not in self._portfolio.initial_cash:
                continue
            positions = []
            for position in self._portfolio.open_positions(venue.id):
                last = self._portfolio.last_price(venue.id, position.symbol)
                positions.append(
                    PositionStatus(
                        symbol=position.symbol,
                        quantity=position.quantity,
                        avg_price=position.avg_price,
                        last_price=last,
                        unrealized=position.unrealized_pnl(last) if last else Decimal(0),
                        stop_loss=position.stop_loss,
                        take_profit=position.take_profit,
                    )
                )
            result.append(
                VenueStatus(
                    venue=venue.id,
                    currency=venue.quote_currency,
                    equity=self._portfolio.equity(venue.id),
                    cash=self._portfolio.cash(venue.id),
                    initial=self._portfolio.initial_cash[venue.id],
                    positions=positions,
                )
            )
        return result

    def scores(self) -> list[tuple[ScoreResult, bool]]:
        """Última nota de cada mercado vigilado con la estrategia de puntuación, y si cumple."""
        result = []
        for (venue, symbol), strategy in self._strategies.items():
            if isinstance(strategy, ScoreStrategy):
                score = strategy.last_result(venue, symbol)
                if score is not None:
                    meets = score.tradable and score.score >= strategy.params.entry_score
                    result.append((score, meets))
        return sorted(result, key=lambda item: item[0].score, reverse=True)

    def today(self) -> DaySummary:
        self._roll()
        today = self._clock.now().astimezone(self._tz).date()
        trades = [
            t
            for t in self._portfolio.closed_trades
            if t.exit_time.astimezone(self._tz).date() == today
        ]
        pnl: dict[str, Decimal] = {}
        for trade in trades:
            currency = self._config.venue(trade.venue).quote_currency
            pnl[currency] = pnl.get(currency, Decimal(0)) + trade.pnl
        return DaySummary(
            trades=len(trades),
            wins=sum(1 for t in trades if t.is_win),
            pnl_by_currency=pnl,
            signals=self._counters.signals,
            approved=self._counters.approved,
            rejections=self._counters.rejections.most_common(5),
        )

    def _on_decision(self, event: RiskDecision) -> None:
        self._roll()
        self._counters.signals += 1
        if event.approved:
            self._counters.approved += 1
        else:
            self._counters.rejections[event.reason] += 1

    def _roll(self) -> None:
        today = self._clock.now().astimezone(self._tz).date()
        if self._counters.day != today:
            self._counters = _DayCounters(day=today)
