"""Ruptura con salida dinámica: compra al superar el máximo de N velas y deja correr.

Aprendizaje que aplica: acertar mucho no es lo importante, sino ganar más cuando se acierta.
La ruptura clásica vende al perder el mínimo de M velas; aquí la salida es un stop que sube
con el precio (Chandelier), sin objetivo fijo.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import Field

from invertio.core.models import Bar, Signal, SignalAction
from invertio.indicators import RollingMax
from invertio.strategies.base import Strategy, StrategyContext, StrategyParams
from invertio.strategies.exits import Chandelier


class TrailingBreakoutParams(StrategyParams):
    entry_bars: int = Field(default=55, ge=2)  # compra al cerrar sobre el máximo de N velas
    stop_atr: float = Field(default=3.0, gt=0)  # stop inicial y distancia del stop que sigue
    trail_bars: int = Field(default=22, ge=2)  # el stop cuelga del máximo de estas velas
    atr_period: int = Field(default=22, ge=2)


@dataclass(slots=True)
class _State:
    highs: RollingMax
    exit: Chandelier
    prev_high: float | None = None


class TrailingBreakoutStrategy(Strategy[TrailingBreakoutParams]):
    id = "ruptura_dinamica"
    params_model = TrailingBreakoutParams

    def __init__(self, params: TrailingBreakoutParams) -> None:
        super().__init__(params)
        self._states: dict[tuple[str, str], _State] = {}

    @property
    def warmup_bars(self) -> int:
        p = self.params
        return max(p.entry_bars, p.trail_bars, p.atr_period + 1) + 1

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[Signal]:
        p = self.params
        key = (bar.venue, bar.symbol)
        state = self._states.get(key)
        if state is None:
            state = _State(RollingMax(p.entry_bars), Chandelier(p.trail_bars, p.atr_period))
            self._states[key] = state
        prev_high = state.prev_high  # máximo de las velas ANTERIORES a esta
        state.prev_high = state.highs.update(bar.high)
        state.exit.update(bar)
        atr, trail = state.exit.atr, state.exit.level(p.stop_atr)
        if prev_high is None or atr is None or trail is None:
            return []
        in_position = ctx.position(bar.venue, bar.symbol).is_open
        stop = bar.close - p.stop_atr * atr
        if not in_position and bar.close > prev_high and stop > 0:
            return [
                Signal(
                    strategy_id=self.id,
                    venue=bar.venue,
                    symbol=bar.symbol,
                    action=SignalAction.BUY,
                    ts=bar.close_time,
                    price=bar.close,
                    stop_loss=stop,
                    reason=f"ruptura del máximo de {p.entry_bars} velas; stop que sigue al precio",
                )
            ]
        if in_position and bar.close < trail:
            return [
                Signal(
                    strategy_id=self.id,
                    venue=bar.venue,
                    symbol=bar.symbol,
                    action=SignalAction.CLOSE,
                    ts=bar.close_time,
                    price=bar.close,
                    reason=(
                        f"cierre bajo el máximo de {p.trail_bars} velas menos "
                        f"{p.stop_atr:g} ATR (stop que sigue al precio)"
                    ),
                )
            ]
        return []
