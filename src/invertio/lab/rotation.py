"""Rotación semanal por momento: una cartera que cada semana se queda con las más fuertes.

Reglas (las mismas en entrenamiento y en test):
- Cada `rebalance_days` días (los lunes con 7), con el cierre diario anterior, se ordenan las
  monedas por su rentabilidad de los últimos `lookback_days` días.
- Se mantienen las `top` primeras con rentabilidad positiva (momento absoluto). Si el filtro
  de BTC está apagado (cierre por debajo de su media de N días), todo a euros.
- Se vende lo que sale del grupo y se compra lo que entra, a la apertura del día (a mercado:
  medio spread + deslizamiento + comisión taker). Lo que sigue dentro no se toca, para no
  pagar costes por reequilibrar pesos.

Limitación conocida: solo se prueban monedas que hoy cotizan en Revolut X. Las que
desaparecieron no están, y eso favorece a las estrategias de momento (sesgo de
supervivencia). Por eso el test fuera de muestra y la observación en vivo son obligatorios.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from invertio.core.models import Bar
from invertio.lab.config import RotationConfig

type Series = dict[datetime, tuple[float, float]]  # apertura del día -> (apertura, cierre)

DAY = timedelta(days=1)


@dataclass(slots=True)
class RotationRun:
    params: dict[str, Any]
    period: str
    start: datetime
    end: datetime
    return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    trades: int = 0
    costs_eur: float = 0.0  # comisiones + spread + deslizamiento
    exposure_pct: float = 0.0  # % de días con alguna moneda en cartera
    btc_return_pct: float = 0.0
    equal_weight_return_pct: float = 0.0
    equity: list[tuple[datetime, float]] = field(default_factory=list)
    log: list[dict[str, Any]] = field(default_factory=list)


def daily_series(bars: list[Bar]) -> Series:
    return {bar.open_time: (bar.open, bar.close) for bar in bars}


def _last_close(series: Series, day: datetime, lookback: int = 10) -> float | None:
    for back in range(lookback):
        values = series.get(day - back * DAY)
        if values is not None:
            return values[1]
    return None


def rank_momentum(
    data: dict[str, Series], day: datetime, lookback: int, tradable: set[str] | None = None
) -> list[tuple[float, str]]:
    """Monedas con rentabilidad positiva en `lookback` días, de mayor a menor.

    Usa el cierre del día anterior a `day` (ya conocido a su apertura) y el de `lookback`
    días antes. Es la misma regla en el laboratorio y en las carteras en vivo.
    """
    signal_day = day - DAY
    ranked = []
    for symbol, series in data.items():
        if tradable is not None and symbol not in tradable:
            continue
        now, past = series.get(signal_day), series.get(signal_day - lookback * DAY)
        if now is None or past is None or past[1] <= 0:
            continue
        momentum = now[1] / past[1] - 1
        if momentum > 0:
            ranked.append((momentum, symbol))
    ranked.sort(reverse=True)
    return ranked


def _open_or_last(series: Series, day: datetime) -> float:
    bar = series.get(day)
    return bar[0] if bar is not None else (_last_close(series, day) or 0.0)


def simulate(
    data: dict[str, Series],
    params: dict[str, Any],
    start: datetime,
    end: datetime,
    *,
    costs: dict[str, float],
    fee_pct: float,
    rebalance_days: int,
    regime: Callable[[datetime], bool] | None,
    period: str,
    btc_symbol: str = "BTC/EUR",
    capital: float = 100.0,
) -> RotationRun:
    """`costs[símbolo]` = coste por lado en % sin comisión (medio spread + deslizamiento)."""
    lookback, top = int(params["lookback_days"]), int(params["top"])
    run = RotationRun(params=dict(params), period=period, start=start, end=end)
    days = sorted({day for series in data.values() for day in series if start <= day < end})
    if not days:
        return run
    fee = fee_pct / 100
    cash = capital
    holdings: dict[str, float] = {}
    peak, invested_days = capital, 0
    for day in days:
        if (day.toordinal() - 1) % rebalance_days == 0:  # con 7 días: los lunes
            ranked = []
            if regime is None or regime(day):
                tradable = {symbol for symbol, series in data.items() if day in series}
                ranked = rank_momentum(data, day, lookback, tradable)
            target = [symbol for _, symbol in ranked[:top]]
            sold, bought = [], []
            for symbol in [s for s in holdings if s not in target]:
                bar = data[symbol].get(day)
                if bar is None:
                    continue  # sin cotización hoy: se venderá en el próximo reequilibrio
                side_cost = costs.get(symbol, 0.0) / 100
                gross = holdings.pop(symbol) * bar[0]
                proceeds = gross * (1 - side_cost) * (1 - fee)
                run.costs_eur += gross - proceeds
                cash += proceeds
                run.trades += 1
                sold.append(symbol)
            equity_open = cash + sum(
                quantity * _open_or_last(data[s], day) for s, quantity in holdings.items()
            )
            new = [symbol for symbol in target if symbol not in holdings]
            for symbol in new:
                amount = min(equity_open / top, cash)
                if amount < 1:  # importe mínimo práctico
                    break
                side_cost = costs.get(symbol, 0.0) / 100
                price = data[symbol][day][0] * (1 + side_cost)
                quantity = amount * (1 - fee) / price
                run.costs_eur += amount - quantity * data[symbol][day][0]
                holdings[symbol] = quantity
                cash -= amount
                run.trades += 1
                bought.append(symbol)
            if sold or bought:
                run.log.append(
                    {"day": day.date().isoformat(), "sold": sold, "bought": bought,
                     "holding": sorted(holdings)}
                )  # fmt: skip
        equity = cash + sum(
            quantity * (_last_close(data[s], day) or 0.0) for s, quantity in holdings.items()
        )
        invested_days += bool(holdings)
        peak = max(peak, equity)
        run.max_drawdown_pct = max(run.max_drawdown_pct, (peak - equity) / peak * 100)
        run.equity.append((day, equity))
    run.return_pct = (run.equity[-1][1] / capital - 1) * 100
    run.exposure_pct = invested_days / len(days) * 100
    first, last = days[0], days[-1]
    btc = data.get(btc_symbol, {})
    if first in btc and last in btc:
        run.btc_return_pct = (btc[last][1] / btc[first][0] - 1) * 100
    holds = [
        series[last][1] / series[first][0] - 1
        for series in data.values()
        if first in series and last in series and series[first][0] > 0
    ]
    run.equal_weight_return_pct = sum(holds) / len(holds) * 100 if holds else 0.0
    return run


@dataclass(slots=True)
class RotationCandidate:
    train_rank: int
    train: RotationRun
    test: RotationRun
    reasons: list[str]

    @property
    def verdict(self) -> str:
        return "no aprueba" if self.reasons else "aprueba"


@dataclass(slots=True)
class RotationResult:
    train_start: datetime
    test_start: datetime
    end: datetime
    symbols: list[str]
    train: list[RotationRun]  # todas las combinaciones, de mejor a peor en entrenamiento
    candidates: list[RotationCandidate]


def run_rotation(
    config: RotationConfig,
    data: dict[str, Series],
    btc_daily: list[Bar],
    *,
    costs: dict[str, float],
    test_start: datetime,
    end: datetime,
    top_candidates: int = 3,
) -> RotationResult:
    """Elige parámetros en entrenamiento y juzga las mejores combinaciones en el test."""
    from invertio.strategies.regime import btc_regime

    regimes = {days: btc_regime(btc_daily, days) for days in config.btc_filter if days}
    first = min(min(series) for series in data.values() if series)
    # Mismo inicio para todas las combinaciones: después del periodo de medida más largo.
    train_start = first + (max(config.lookback_days) + 1) * DAY

    def run(params: dict[str, Any], start: datetime, stop: datetime, period: str) -> RotationRun:
        days = params["btc_filter"]
        return simulate(
            data, params, start, stop, costs=costs, fee_pct=config.fee_pct,
            rebalance_days=config.rebalance_days, regime=regimes.get(days) if days else None,
            period=period,
        )  # fmt: skip

    train = [run(params, train_start, test_start, "train") for params in config.combinations()]
    train.sort(key=lambda r: (r.return_pct, -r.max_drawdown_pct), reverse=True)
    candidates = []
    for rank, best in enumerate(train[:top_candidates], start=1):
        test = run(best.params, test_start, end, "test")
        candidates.append(RotationCandidate(rank, best, test, judge(best, test, config)))
    return RotationResult(train_start, test_start, end, sorted(data), train, candidates)


def judge(train: RotationRun, test: RotationRun, config: RotationConfig) -> list[str]:
    """Motivos de suspenso; lista vacía = aprueba. Reglas fijadas en lab.yaml de antemano."""
    verdict = config.verdict
    reasons = []
    if train.return_pct <= verdict.min_return_pct:
        reasons.append(f"entrenamiento {train.return_pct:+.1f} %")
    if test.return_pct <= verdict.min_return_pct:
        reasons.append(f"test {test.return_pct:+.1f} %")
    if verdict.beat_btc and test.return_pct <= test.btc_return_pct:
        reasons.append(
            f"no supera a mantener BTC en test ({test.return_pct:+.1f} % frente a "
            f"{test.btc_return_pct:+.1f} %)"
        )
    if test.trades < verdict.min_trades:
        reasons.append(f"pocas operaciones en test ({test.trades})")
    return reasons
