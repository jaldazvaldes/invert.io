"""Métricas de un backtest."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from invertio.backtest.engine import BacktestResult

YEAR = timedelta(days=365.25)


@dataclass(frozen=True, slots=True)
class Metrics:
    total_return_pct: float
    buy_and_hold_pct: float
    max_drawdown_pct: float
    sharpe: float
    trades: int
    win_rate_pct: float
    profit_factor: float | None  # None si no hubo pérdidas
    avg_trade_pct: float
    fees_paid: float
    exposure_pct: float  # % del tiempo con posición abierta

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _pct(numerator: Decimal, denominator: Decimal) -> float:
    return float(numerator / denominator * 100) if denominator else 0.0


def max_drawdown_pct(equity: list[Decimal]) -> float:
    peak = Decimal(0)
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, float((peak - value) / peak * 100))
    return worst


def sharpe_ratio(result: BacktestResult) -> float:
    """Sharpe anualizado a partir de los rendimientos por vela (tipo libre de riesgo = 0)."""
    curve = result.equity_curve
    if len(curve) < 3:
        return 0.0
    returns = [
        float(curve[i][1] / curve[i - 1][1] - 1) for i in range(1, len(curve)) if curve[i - 1][1]
    ]
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    if variance <= 0:
        return 0.0
    years = (curve[-1][0] - curve[0][0]) / YEAR
    periods_per_year = len(returns) / years if years > 0 else 0
    return mean / math.sqrt(variance) * math.sqrt(periods_per_year)


def compute_metrics(result: BacktestResult) -> Metrics:
    trades = result.trades
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [-t.pnl for t in trades if t.pnl <= 0]
    gross_loss = sum(losses, Decimal(0))
    fees = sum((t.fees for t in trades), Decimal(0))
    span = result.equity_curve[-1][0] - result.equity_curve[0][0] if result.equity_curve else None
    in_market = sum((t.exit_time - t.entry_time for t in trades), timedelta())
    return Metrics(
        total_return_pct=_pct(result.final_equity - result.initial_cash, result.initial_cash),
        buy_and_hold_pct=(result.last_price / result.first_price - 1) * 100,
        max_drawdown_pct=max_drawdown_pct([e for _, e in result.equity_curve]),
        sharpe=sharpe_ratio(result),
        trades=len(trades),
        win_rate_pct=len(wins) / len(trades) * 100 if trades else 0.0,
        profit_factor=float(sum(wins, Decimal(0)) / gross_loss) if gross_loss else None,
        avg_trade_pct=sum(t.return_pct for t in trades) / len(trades) if trades else 0.0,
        fees_paid=float(fees),
        exposure_pct=in_market / span * 100 if span else 0.0,
    )
