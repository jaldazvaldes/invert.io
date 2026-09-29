"""Retroceso en tendencia: compra una caída corta dentro de una subida clara.

Aprendizaje que aplica: lo que más ayudó fue no comprar contra la tendencia. Solo se compra
con el precio sobre su media de 200 velas y la media de 50 por encima de la de 200; la
entrada es un rebote tras una caída corta (RSI de 3 velas) y la salida, el rebote hasta el
nivel de salida del RSI o el stop que sigue al precio (Chandelier).
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import Field, model_validator

from invertio.core.models import Bar, Signal, SignalAction
from invertio.indicators import EMA, RSI
from invertio.strategies.base import Strategy, StrategyContext, StrategyParams
from invertio.strategies.exits import Chandelier


class PullbackParams(StrategyParams):
    fast_ema: int = Field(default=50, ge=2)
    slow_ema: int = Field(default=200, ge=3)
    rsi_period: int = Field(default=3, ge=2)
    dip: float = Field(default=20, gt=0, lt=50)  # el RSI corto vuelve a subir de este nivel
    exit_rsi: float = Field(default=70, gt=50, lt=100)
    stop_atr: float = Field(default=3.0, gt=0)
    trail_bars: int = Field(default=22, ge=2)

    @model_validator(mode="after")
    def _periods(self) -> PullbackParams:
        if self.fast_ema >= self.slow_ema:
            raise ValueError("fast_ema debe ser menor que slow_ema")
        return self


@dataclass(slots=True)
class _State:
    fast: EMA
    slow: EMA
    rsi: RSI
    exit: Chandelier
    prev_rsi: float | None = None


class PullbackStrategy(Strategy[PullbackParams]):
    id = "retroceso"
    params_model = PullbackParams

    def __init__(self, params: PullbackParams) -> None:
        super().__init__(params)
        self._states: dict[tuple[str, str], _State] = {}

    @property
    def warmup_bars(self) -> int:
        p = self.params
        return max(p.slow_ema, p.trail_bars, 23) + 2

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[Signal]:
        p = self.params
        key = (bar.venue, bar.symbol)
        state = self._states.get(key)
        if state is None:
            state = _State(
                EMA(p.fast_ema), EMA(p.slow_ema), RSI(p.rsi_period), Chandelier(p.trail_bars, 22)
            )
            self._states[key] = state
        fast = state.fast.update(bar.close)
        slow = state.slow.update(bar.close)
        prev_rsi, rsi = state.prev_rsi, state.rsi.update(bar.close)
        state.prev_rsi = rsi
        state.exit.update(bar)
        atr, trail = state.exit.atr, state.exit.level(p.stop_atr)
        if None in (fast, slow, prev_rsi, rsi, atr, trail):
            return []
        assert fast is not None and slow is not None and atr is not None and trail is not None
        assert prev_rsi is not None and rsi is not None
        in_position = ctx.position(bar.venue, bar.symbol).is_open
        uptrend = bar.close > slow and fast > slow
        rebound = prev_rsi < p.dip <= rsi
        stop = bar.close - p.stop_atr * atr
        if not in_position and uptrend and rebound and stop > 0:
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
                        f"rebote tras una caída corta (RSI {p.rsi_period} recupera {p.dip:g}) "
                        f"con tendencia alcista (EMA {p.fast_ema} > EMA {p.slow_ema})"
                    ),
                )
            ]
        if in_position and (rsi >= p.exit_rsi or bar.close < trail):
            reason = (
                f"RSI {p.rsi_period} alcanza {rsi:.0f} (salida con {p.exit_rsi:g})"
                if rsi >= p.exit_rsi
                else f"cierre bajo el stop que sigue al precio ({p.stop_atr:g} ATR)"
            )
            return [
                Signal(
                    strategy_id=self.id,
                    venue=bar.venue,
                    symbol=bar.symbol,
                    action=SignalAction.CLOSE,
                    ts=bar.close_time,
                    price=bar.close,
                    reason=reason,
                )
            ]
        return []
