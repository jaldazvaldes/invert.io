"""Seguimiento de tendencia: comprado mientras el precio está sobre una media que sube."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from pydantic import Field

from invertio.core.models import Bar, Signal, SignalAction
from invertio.indicators import ATR, EMA
from invertio.strategies.base import Strategy, StrategyContext, StrategyParams


class TrendParams(StrategyParams):
    ema: int = Field(default=100, ge=5)
    slope_bars: int = Field(default=10, ge=1)  # la media debe subir respecto a hace N velas
    exit_buffer_atr: float = Field(default=1.0, ge=0)  # margen bajo la media antes de salir
    stop_atr: float = Field(default=4.0, gt=0)  # stop de emergencia
    atr_period: int = Field(default=14, ge=2)


@dataclass(slots=True)
class _State:
    ema: EMA
    atr: ATR
    history: deque[float]
    was_uptrend: bool | None = field(default=None)


class TrendStrategy(Strategy[TrendParams]):
    """Entra cuando empieza una tendencia alcista (precio sobre la EMA y EMA subiendo) y sale
    cuando el precio cae claramente por debajo de la EMA."""

    id = "tendencia"
    params_model = TrendParams

    def __init__(self, params: TrendParams) -> None:
        super().__init__(params)
        self._states: dict[tuple[str, str], _State] = {}

    @property
    def warmup_bars(self) -> int:
        return self.params.ema + self.params.slope_bars + 1

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[Signal]:
        p = self.params
        key = (bar.venue, bar.symbol)
        state = self._states.get(key)
        if state is None:
            state = _State(EMA(p.ema), ATR(p.atr_period), deque(maxlen=p.slope_bars + 1))
            self._states[key] = state
        ema = state.ema.update(bar.close)
        atr = state.atr.update(bar.high, bar.low, bar.close)
        if ema is None:
            return []
        state.history.append(ema)
        if atr is None or len(state.history) <= p.slope_bars:
            return []

        uptrend = bar.close > ema and ema > state.history[0]
        was_uptrend, state.was_uptrend = state.was_uptrend, uptrend
        in_position = ctx.position(bar.venue, bar.symbol).is_open
        stop_ok = atr > 0 and bar.close > p.stop_atr * atr  # stop positivo: riesgo acotado
        if not in_position and uptrend and was_uptrend is False and stop_ok:
            return [
                Signal(
                    strategy_id=self.id,
                    venue=bar.venue,
                    symbol=bar.symbol,
                    action=SignalAction.BUY,
                    ts=bar.close_time,
                    price=bar.close,
                    stop_loss=bar.close - p.stop_atr * atr,
                    reason=f"empieza tendencia alcista sobre EMA{p.ema}",
                )
            ]
        if in_position and bar.close < ema - p.exit_buffer_atr * atr:
            return [
                Signal(
                    strategy_id=self.id,
                    venue=bar.venue,
                    symbol=bar.symbol,
                    action=SignalAction.CLOSE,
                    ts=bar.close_time,
                    price=bar.close,
                    reason=f"precio por debajo de EMA{p.ema}",
                )
            ]
        return []
