"""Conecta las velas cerradas con la estrategia asignada a cada mercado."""

from __future__ import annotations

from typing import Any

from invertio.core.bus import EventBus
from invertio.core.events import BarClosed, SignalEmitted
from invertio.core.models import SignalAction
from invertio.portfolio import Portfolio
from invertio.strategies.base import Strategy

type MarketKey = tuple[str, str]  # (venue, símbolo)


class StrategyRunner:
    """Pasa cada vela cerrada a la estrategia de su mercado y publica sus señales.

    Se debe crear después del portfolio: así, al cerrar una vela, el portfolio ya conoce el
    último precio cuando la estrategia y el gestor de riesgo reciben el evento.
    """

    def __init__(
        self, bus: EventBus, portfolio: Portfolio, assignments: dict[MarketKey, Strategy[Any]]
    ) -> None:
        self._bus = bus
        self._portfolio = portfolio
        self.assignments = dict(assignments)
        # Mercados desactivados: no abren posiciones, pero sí pueden cerrarlas.
        self.disabled: set[MarketKey] = set()
        bus.subscribe(BarClosed, self._on_bar)

    async def _on_bar(self, event: BarClosed) -> None:
        strategy = self.assignments.get((event.bar.venue, event.bar.symbol))
        if strategy is None:
            return
        signals = strategy.on_bar(event.bar, self._portfolio)
        if not event.live:
            return  # calentamiento: los indicadores avanzan, pero no se opera
        disabled = (event.bar.venue, event.bar.symbol) in self.disabled
        for signal in signals:
            if disabled and signal.action is SignalAction.BUY:
                continue
            await self._bus.publish(SignalEmitted(signal))
