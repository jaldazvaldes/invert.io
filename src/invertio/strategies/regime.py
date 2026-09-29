"""Filtro de mercado: solo se abren posiciones cuando BTC está en tendencia alcista.

Casi todas las criptomonedas caen a la vez que BTC. El filtro compara el cierre diario de
BTC con su media simple de N días; con el cierre por debajo (o sin historia suficiente) se
descartan las compras de cualquier estrategia. Las salidas nunca se bloquean.

Sin mirar al futuro: para una vela que cierra en `t` se usa el último día de BTC cerrado
en `t` o antes.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from invertio.core.models import Bar, Signal, SignalAction
from invertio.strategies.base import Strategy, StrategyContext, StrategyParams

type Regime = Callable[[datetime], bool]


def btc_regime(daily: Sequence[Bar], days: int) -> Regime:
    """Función `t -> bool`: ¿cerró el último día completo de BTC por encima de su media?"""
    if days < 2:
        raise ValueError("La media del filtro necesita al menos 2 días")
    ordered = sorted(daily, key=lambda bar: bar.open_time)
    closes_at: list[datetime] = []
    bullish: list[bool] = []
    window: list[float] = []
    total = 0.0
    for bar in ordered:
        window.append(bar.close)
        total += bar.close
        if len(window) > days:
            total -= window.pop(0)
        closes_at.append(bar.close_time)
        bullish.append(len(window) == days and bar.close > total / days)

    def allowed(at: datetime) -> bool:
        index = bisect.bisect_right(closes_at, at) - 1
        return index >= 0 and bullish[index]

    return allowed


class RegimeFiltered(Strategy[StrategyParams]):
    """Envuelve una estrategia y descarta sus compras cuando el filtro está apagado."""

    id = "filtro_btc"
    params_model = StrategyParams

    def __init__(self, inner: Strategy[Any], allowed: Regime) -> None:
        super().__init__(StrategyParams())
        self.inner = inner
        self.allowed = allowed

    @property
    def warmup_bars(self) -> int:
        return self.inner.warmup_bars

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[Signal]:
        # La estrategia interna avanza siempre sus indicadores; decide por su posición real,
        # así que una compra descartada no la deja creyendo que ha comprado.
        signals = self.inner.on_bar(bar, ctx)
        if self.allowed(bar.close_time):
            return signals
        return [signal for signal in signals if signal.action is not SignalAction.BUY]
