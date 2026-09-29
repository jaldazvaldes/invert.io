"""Compresión y expansión: compra la ruptura que llega después de una etapa de calma.

Aprendizaje que aplica: la volatilidad se agrupa y es lo más predecible del precio. Tras
velas estrechas suelen llegar velas amplias; se compra solo si esa expansión es al alza y se
sale con un stop que sube con el precio (Chandelier).
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import Field, model_validator

from invertio.core.models import Bar, Signal, SignalAction
from invertio.indicators import ATR, RollingMax
from invertio.strategies.base import Strategy, StrategyContext, StrategyParams
from invertio.strategies.exits import Chandelier


class SqueezeParams(StrategyParams):
    fast_atr: int = Field(default=5, ge=2)
    slow_atr: int = Field(default=50, ge=3)
    # Calma: el ATR corto por debajo de esta fracción del largo en alguna de las últimas velas.
    squeeze_ratio: float = Field(default=0.75, gt=0, lt=1)
    squeeze_window: int = Field(default=10, ge=1)
    breakout_bars: int = Field(default=20, ge=2)
    stop_atr: float = Field(default=3.0, gt=0)
    trail_bars: int = Field(default=22, ge=2)

    @model_validator(mode="after")
    def _periods(self) -> SqueezeParams:
        if self.fast_atr >= self.slow_atr:
            raise ValueError("fast_atr debe ser menor que slow_atr")
        return self


@dataclass(slots=True)
class _State:
    fast: ATR
    slow: ATR
    highs: RollingMax
    exit: Chandelier
    prev_high: float | None = None
    bars_since_squeeze: int | None = None


class SqueezeStrategy(Strategy[SqueezeParams]):
    id = "compresion"
    params_model = SqueezeParams

    def __init__(self, params: SqueezeParams) -> None:
        super().__init__(params)
        self._states: dict[tuple[str, str], _State] = {}

    @property
    def warmup_bars(self) -> int:
        p = self.params
        return max(p.slow_atr + 1, p.breakout_bars, p.trail_bars, 23) + 1

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[Signal]:
        p = self.params
        key = (bar.venue, bar.symbol)
        state = self._states.get(key)
        if state is None:
            state = _State(
                ATR(p.fast_atr), ATR(p.slow_atr), RollingMax(p.breakout_bars),
                Chandelier(p.trail_bars, 22),
            )  # fmt: skip
            self._states[key] = state
        prev_high = state.prev_high
        state.prev_high = state.highs.update(bar.high)
        fast = state.fast.update(bar.high, bar.low, bar.close)
        slow = state.slow.update(bar.high, bar.low, bar.close)
        state.exit.update(bar)
        if fast is not None and slow is not None and slow > 0 and fast < p.squeeze_ratio * slow:
            state.bars_since_squeeze = 0
        elif state.bars_since_squeeze is not None:
            state.bars_since_squeeze += 1
        atr, trail = state.exit.atr, state.exit.level(p.stop_atr)
        if prev_high is None or atr is None or trail is None or slow is None:
            return []
        in_position = ctx.position(bar.venue, bar.symbol).is_open
        squeezed = (
            state.bars_since_squeeze is not None and state.bars_since_squeeze <= p.squeeze_window
        )
        stop = bar.close - p.stop_atr * atr
        if not in_position and squeezed and bar.close > prev_high and stop > 0:
            return [
                Signal(
                    strategy_id=self.id,
                    venue=bar.venue,
                    symbol=bar.symbol,
                    action=SignalAction.BUY,
                    ts=bar.close_time,
                    price=bar.close,
                    stop_loss=stop,
                    reason=(
                        f"ruptura del máximo de {p.breakout_bars} velas tras una etapa de "
                        "calma (volatilidad comprimida)"
                    ),
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
                    reason=f"cierre bajo el stop que sigue al precio ({p.stop_atr:g} ATR)",
                )
            ]
        return []
