"""Reversión a la media con RSI y filtro de tendencia (estrategia de ejemplo)."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import Field, model_validator

from invertio.core.models import Bar, Signal, SignalAction
from invertio.indicators import ATR, EMA, RSI
from invertio.strategies.base import Strategy, StrategyContext, StrategyParams


class RsiReversionParams(StrategyParams):
    rsi_period: int = Field(default=14, ge=2)
    oversold: float = Field(default=30, gt=0, lt=100)
    exit_above: float = Field(default=55, gt=0, lt=100)
    atr_period: int = Field(default=14, ge=2)
    stop_atr: float = Field(default=2.0, gt=0)
    take_profit_atr: float | None = Field(default=None, gt=0)
    trend_ema: int | None = Field(default=200, ge=2)  # solo compra si el precio está por encima

    @model_validator(mode="after")
    def _levels(self) -> RsiReversionParams:
        if self.oversold >= self.exit_above:
            raise ValueError("oversold debe ser menor que exit_above")
        return self


@dataclass(slots=True)
class _State:
    rsi: RSI
    atr: ATR
    trend: EMA | None
    prev_rsi: float | None = None


class RsiReversion(Strategy[RsiReversionParams]):
    """Compra cuando el RSI sale de sobreventa (con tendencia alcista) y cierra al recuperarse."""

    id = "rsi_reversion"
    params_model = RsiReversionParams

    def __init__(self, params: RsiReversionParams) -> None:
        super().__init__(params)
        self._states: dict[tuple[str, str], _State] = {}

    @property
    def warmup_bars(self) -> int:
        p = self.params
        return max(p.rsi_period + 1, p.atr_period + 1, p.trend_ema or 0) + 1

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[Signal]:
        p = self.params
        state = self._states.get((bar.venue, bar.symbol))
        if state is None:
            state = _State(
                RSI(p.rsi_period), ATR(p.atr_period), EMA(p.trend_ema) if p.trend_ema else None
            )
            self._states[(bar.venue, bar.symbol)] = state
        rsi = state.rsi.update(bar.close)
        atr = state.atr.update(bar.high, bar.low, bar.close)
        trend = state.trend.update(bar.close) if state.trend else None
        prev, state.prev_rsi = state.prev_rsi, rsi
        if rsi is None or atr is None or prev is None or (state.trend and trend is None):
            return []

        in_position = ctx.position(bar.venue, bar.symbol).is_open
        uptrend = trend is None or bar.close > trend
        if not in_position and uptrend and prev < p.oversold <= rsi and atr > 0:
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
                    reason=f"RSI sale de sobreventa ({rsi:.1f})",
                )
            ]
        if in_position and rsi >= p.exit_above:
            return [
                Signal(
                    strategy_id=self.id,
                    venue=bar.venue,
                    symbol=bar.symbol,
                    action=SignalAction.CLOSE,
                    ts=bar.close_time,
                    price=bar.close,
                    reason=f"RSI recuperado ({rsi:.1f})",
                )
            ]
        return []
