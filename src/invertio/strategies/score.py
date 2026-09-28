"""Estrategia por puntuación: combina varias señales técnicas en una nota de 0 a 100.

Cada factor aporta entre 0 y su peso. Los pesos y umbrales están en
config/strategies/puntuacion.yaml y se pueden ajustar. Compra cuando la nota cruza hacia
arriba el umbral de entrada (y el movimiento esperado cubre los costes) y cierra cuando cae
por debajo del umbral de salida, o antes si saltan el stop o el objetivo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from pydantic import Field, model_validator

from invertio.core.models import Bar, Signal, SignalAction
from invertio.indicators import ATR, EMA, MACD, ROC, RSI, SMA, RollingMax
from invertio.strategies.base import Strategy, StrategyContext, StrategyParams

FACTORS = ("tendencia", "momentum", "macd", "rsi", "volumen", "ruptura")


def points_str(value: float) -> str:
    """92.5 → "92,5"; 100.0 → "100"."""
    return f"{value:g}".replace(".", ",")


class ScoreWeights(StrategyParams):
    tendencia: int = Field(default=25, ge=0)
    momentum: int = Field(default=20, ge=0)
    macd: int = Field(default=15, ge=0)
    rsi: int = Field(default=15, ge=0)
    volumen: int = Field(default=10, ge=0)
    ruptura: int = Field(default=15, ge=0)

    @model_validator(mode="after")
    def _sum_100(self) -> ScoreWeights:
        total = sum(getattr(self, f) for f in FACTORS)
        if total != 100:
            raise ValueError(f"Los pesos deben sumar 100 (suman {total})")
        return self


class ScoreParams(StrategyParams):
    weights: ScoreWeights = ScoreWeights()
    ema_fast: int = Field(default=50, ge=2)
    ema_trend: int = Field(default=200, ge=3)
    roc_bars: int = Field(default=12, ge=1)
    rsi_period: int = Field(default=14, ge=2)
    volume_period: int = Field(default=20, ge=2)
    breakout_bars: int = Field(default=48, ge=2)
    atr_period: int = Field(default=14, ge=2)
    entry_score: float = Field(default=70, gt=0, le=100)
    exit_score: float = Field(default=40, ge=0, lt=100)
    # Filtro de costes: el ATR (movimiento típico por vela) debe ser al menos este % del precio.
    min_atr_pct: float = Field(default=0.15, ge=0)
    stop_atr: float = Field(default=2.0, gt=0)
    take_profit_atr: float | None = Field(default=3.0, gt=0)

    @model_validator(mode="after")
    def _consistent(self) -> ScoreParams:
        if self.ema_fast >= self.ema_trend:
            raise ValueError("ema_fast debe ser menor que ema_trend")
        if self.exit_score >= self.entry_score:
            raise ValueError("exit_score debe ser menor que entry_score")
        return self


@dataclass(frozen=True, slots=True)
class ScoreResult:
    """Nota de un mercado en una vela, con el desglose por factor."""

    venue: str
    symbol: str
    ts: datetime
    close: float
    score: float
    points: dict[str, float]
    max_points: dict[str, int]
    atr: float
    atr_pct: float
    tradable: bool  # el movimiento típico supera el mínimo que cubre los costes
    stop_loss: float
    take_profit: float | None

    def breakdown(self) -> str:
        return " · ".join(
            f"{name} {points_str(self.points[name])}/{self.max_points[name]}" for name in FACTORS
        )


@dataclass(slots=True)
class _State:
    ema_fast: EMA
    ema_trend: EMA
    roc: ROC
    macd: MACD
    rsi: RSI
    volume: SMA
    highs: RollingMax
    atr: ATR
    prev_histogram: float | None = None
    prev_max_high: float | None = None
    prev_score: float | None = None
    last: ScoreResult | None = field(default=None)


class ScoreStrategy(Strategy[ScoreParams]):
    id = "puntuacion"
    params_model = ScoreParams

    def __init__(self, params: ScoreParams) -> None:
        super().__init__(params)
        self._states: dict[tuple[str, str], _State] = {}

    @property
    def warmup_bars(self) -> int:
        p = self.params
        return max(p.ema_trend, p.breakout_bars + 1, 26 + 9, p.volume_period, p.roc_bars + 1) + 1

    def last_result(self, venue: str, symbol: str) -> ScoreResult | None:
        state = self._states.get((venue, symbol))
        return state.last if state else None

    def evaluate(self, bar: Bar) -> ScoreResult | None:
        """Actualiza los indicadores con `bar` y devuelve la nota (None mientras calienta)."""
        p = self.params
        state = self._states.get((bar.venue, bar.symbol))
        if state is None:
            state = _State(
                EMA(p.ema_fast),
                EMA(p.ema_trend),
                ROC(p.roc_bars),
                MACD(),
                RSI(p.rsi_period),
                SMA(p.volume_period),
                RollingMax(p.breakout_bars),
                ATR(p.atr_period),
            )
            self._states[(bar.venue, bar.symbol)] = state

        prev_max_high = state.prev_max_high  # máximo de las N velas ANTERIORES a esta
        prev_histogram = state.prev_histogram
        ema_fast = state.ema_fast.update(bar.close)
        ema_trend = state.ema_trend.update(bar.close)
        roc = state.roc.update(bar.close)
        histogram = state.macd.update(bar.close)
        rsi = state.rsi.update(bar.close)
        avg_volume = state.volume.update(bar.volume)
        state.prev_max_high = state.highs.update(bar.high)
        atr = state.atr.update(bar.high, bar.low, bar.close)
        state.prev_histogram = histogram

        values = (ema_fast, ema_trend, roc, histogram, rsi, avg_volume, atr)
        if any(v is None for v in values) or prev_max_high is None or prev_histogram is None:
            return None
        assert ema_fast is not None and ema_trend is not None and roc is not None
        assert histogram is not None and rsi is not None and avg_volume is not None
        assert atr is not None

        atr_pct = atr / bar.close * 100
        fractions = {
            "tendencia": 0.6 * (bar.close > ema_trend) + 0.4 * (ema_fast > ema_trend),
            "momentum": 0.5 * (roc > 0) + 0.5 * (roc > atr_pct),
            "macd": 0.5 * (histogram > 0) + 0.5 * (histogram > prev_histogram),
            "rsi": 1.0 if 50 <= rsi <= 70 else 0.5 if 45 <= rsi <= 75 else 0.0,
            "volumen": (
                1.0 if bar.volume > 1.2 * avg_volume else 0.5 if bar.volume > avg_volume else 0.0
            ),
            "ruptura": (
                1.0
                if bar.close > prev_max_high
                else 0.5
                if bar.close >= prev_max_high - 0.5 * atr
                else 0.0
            ),
        }
        weights = {name: getattr(p.weights, name) for name in FACTORS}
        points = {name: round(weights[name] * fractions[name], 1) for name in FACTORS}
        result = ScoreResult(
            venue=bar.venue,
            symbol=bar.symbol,
            ts=bar.close_time,
            close=bar.close,
            score=round(sum(points.values()), 1),
            points=points,
            max_points=weights,
            atr=atr,
            atr_pct=atr_pct,
            tradable=atr_pct >= p.min_atr_pct,
            stop_loss=bar.close - p.stop_atr * atr,
            take_profit=bar.close + p.take_profit_atr * atr if p.take_profit_atr else None,
        )
        state.last = result
        return result

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[Signal]:
        p = self.params
        state_before = self._states.get((bar.venue, bar.symbol))
        prev_score = state_before.prev_score if state_before else None
        result = self.evaluate(bar)
        state = self._states[(bar.venue, bar.symbol)]
        state.prev_score = result.score if result else None
        if result is None or prev_score is None:
            return []

        in_position = ctx.position(bar.venue, bar.symbol).is_open
        crossed_up = prev_score < p.entry_score <= result.score
        if not in_position and crossed_up and result.tradable and result.atr > 0:
            return [
                Signal(
                    strategy_id=self.id,
                    venue=bar.venue,
                    symbol=bar.symbol,
                    action=SignalAction.BUY,
                    ts=bar.close_time,
                    price=bar.close,
                    stop_loss=result.stop_loss,
                    take_profit=result.take_profit,
                    reason=f"puntuación {points_str(result.score)}/100 ({result.breakdown()})",
                )
            ]
        if in_position and result.score <= p.exit_score:
            return [
                Signal(
                    strategy_id=self.id,
                    venue=bar.venue,
                    symbol=bar.symbol,
                    action=SignalAction.CLOSE,
                    ts=bar.close_time,
                    price=bar.close,
                    reason=f"puntuación baja a {points_str(result.score)}/100",
                )
            ]
        return []
