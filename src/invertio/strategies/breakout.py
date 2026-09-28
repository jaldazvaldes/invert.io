"""Rupturas de Donchian (estilo «tortugas»): compra al superar el máximo de N velas."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import Field, model_validator

from invertio.core.models import Bar, Signal, SignalAction
from invertio.indicators import ATR, RollingMax, RollingMin
from invertio.strategies.base import Strategy, StrategyContext, StrategyParams


class BreakoutParams(StrategyParams):
    entry_bars: int = Field(default=55, ge=2)  # compra al cerrar sobre el máximo de N velas
    exit_bars: int = Field(default=20, ge=2)  # vende al cerrar bajo el mínimo de M velas
    stop_atr: float = Field(default=3.0, gt=0)
    atr_period: int = Field(default=20, ge=2)

    @model_validator(mode="after")
    def _exit_shorter(self) -> BreakoutParams:
        if self.exit_bars >= self.entry_bars:
            raise ValueError("exit_bars debe ser menor que entry_bars")
        return self


@dataclass(slots=True)
class _State:
    highs: RollingMax
    lows: RollingMin
    atr: ATR
    prev_high: float | None = None
    prev_low: float | None = None


class BreakoutStrategy(Strategy[BreakoutParams]):
    id = "ruptura"
    params_model = BreakoutParams

    def __init__(self, params: BreakoutParams) -> None:
        super().__init__(params)
        self._states: dict[tuple[str, str], _State] = {}

    @property
    def warmup_bars(self) -> int:
        return max(self.params.entry_bars, self.params.atr_period + 1) + 1

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[Signal]:
        p = self.params
        key = (bar.venue, bar.symbol)
        state = self._states.get(key)
        if state is None:
            state = _State(RollingMax(p.entry_bars), RollingMin(p.exit_bars), ATR(p.atr_period))
            self._states[key] = state
        # Se compara con el canal de las velas ANTERIORES (sin incluir la actual).
        prev_high, prev_low = state.prev_high, state.prev_low
        state.prev_high = state.highs.update(bar.high)
        state.prev_low = state.lows.update(bar.low)
        atr = state.atr.update(bar.high, bar.low, bar.close)
        if prev_high is None or prev_low is None or atr is None or atr <= 0:
            return []

        in_position = ctx.position(bar.venue, bar.symbol).is_open
        stop_ok = bar.close > p.stop_atr * atr  # stop positivo: riesgo acotado
        if not in_position and bar.close > prev_high and stop_ok:
            return [
                Signal(
                    strategy_id=self.id,
                    venue=bar.venue,
                    symbol=bar.symbol,
                    action=SignalAction.BUY,
                    ts=bar.close_time,
                    price=bar.close,
                    stop_loss=bar.close - p.stop_atr * atr,
                    reason=f"ruptura del máximo de {p.entry_bars} velas",
                )
            ]
        if in_position and bar.close < prev_low:
            return [
                Signal(
                    strategy_id=self.id,
                    venue=bar.venue,
                    symbol=bar.symbol,
                    action=SignalAction.CLOSE,
                    ts=bar.close_time,
                    price=bar.close,
                    reason=f"cierre bajo el mínimo de {p.exit_bars} velas",
                )
            ]
        return []
