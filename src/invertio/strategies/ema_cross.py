"""Cruce de medias exponenciales con stop por ATR (estrategia de ejemplo, de tendencia)."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import Field, model_validator

from invertio.core.models import Bar, Signal, SignalAction
from invertio.indicators import ATR, EMA
from invertio.strategies.base import Strategy, StrategyContext, StrategyParams


class EmaCrossParams(StrategyParams):
    fast: int = Field(default=12, ge=2)
    slow: int = Field(default=26, ge=3)
    atr_period: int = Field(default=14, ge=2)
    stop_atr: float = Field(default=2.0, gt=0)
    take_profit_atr: float | None = Field(default=3.0, gt=0)

    @model_validator(mode="after")
    def _fast_below_slow(self) -> EmaCrossParams:
        if self.fast >= self.slow:
            raise ValueError("La EMA rápida debe tener un periodo menor que la lenta")
        return self


@dataclass(slots=True)
class _State:
    fast: EMA
    slow: EMA
    atr: ATR
    prev_diff: float | None = field(default=None)


class EmaCross(Strategy[EmaCrossParams]):
    """Compra cuando la EMA rápida cruza por encima de la lenta y cierra al cruce contrario."""

    id = "ema_cross"
    params_model = EmaCrossParams

    def __init__(self, params: EmaCrossParams) -> None:
        super().__init__(params)
        self._states: dict[tuple[str, str], _State] = {}

    @property
    def warmup_bars(self) -> int:
        return max(self.params.slow, self.params.atr_period + 1) + 1

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[Signal]:
        p = self.params
        state = self._states.get((bar.venue, bar.symbol))
        if state is None:
            state = _State(EMA(p.fast), EMA(p.slow), ATR(p.atr_period))
            self._states[(bar.venue, bar.symbol)] = state
        fast = state.fast.update(bar.close)
        slow = state.slow.update(bar.close)
        atr = state.atr.update(bar.high, bar.low, bar.close)
        if fast is None or slow is None or atr is None:
            return []

        diff = fast - slow
        prev, state.prev_diff = state.prev_diff, diff
        if prev is None:
            return []

        in_position = ctx.position(bar.venue, bar.symbol).is_open
        if not in_position and prev <= 0 < diff and atr > 0:
            return [
                Signal(
                    strategy_id=self.id,
                    venue=bar.venue,
                    symbol=bar.symbol,
                    action=SignalAction.BUY,
                    ts=bar.close_time,
                    price=bar.close,
                    stop_loss=bar.close - p.stop_atr * atr,
                    take_profit=(
                        bar.close + p.take_profit_atr * atr if p.take_profit_atr else None
                    ),
                    reason=f"EMA{p.fast} cruza por encima de EMA{p.slow}",
                )
            ]
        if in_position and prev >= 0 > diff:
            return [
                Signal(
                    strategy_id=self.id,
                    venue=bar.venue,
                    symbol=bar.symbol,
                    action=SignalAction.CLOSE,
                    ts=bar.close_time,
                    price=bar.close,
                    reason=f"EMA{p.fast} cruza por debajo de EMA{p.slow}",
                )
            ]
        return []
