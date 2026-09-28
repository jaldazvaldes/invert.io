from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from invertio.backtest.engine import run_backtest
from invertio.backtest.metrics import compute_metrics, max_drawdown_pct
from invertio.backtest.report import MarketReport, save_reports
from invertio.core.models import Bar, Signal, SignalAction
from invertio.strategies import Strategy, StrategyContext, StrategyParams
from invertio.strategies.ema_cross import EmaCross, EmaCrossParams
from tests.helpers import make_bars, repo_config, with_venue

D = Decimal


class Scripted(Strategy[StrategyParams]):
    """Compra en la vela `buy_at` y cierra en `close_at` (índices de vela)."""

    id = "scripted"
    params_model = StrategyParams

    def __init__(self, buy_at: int, close_at: int, stop_pct: float = 5) -> None:
        super().__init__(StrategyParams())
        self.buy_at, self.close_at, self.stop_pct = buy_at, close_at, stop_pct
        self.seen = 0

    @property
    def warmup_bars(self) -> int:
        return 0

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[Signal]:
        index, self.seen = self.seen, self.seen + 1
        if index == self.buy_at:
            stop = bar.close * (1 - self.stop_pct / 100)
            return [
                Signal(self.id, bar.venue, bar.symbol, SignalAction.BUY, bar.close_time,
                       bar.close, stop_loss=stop)
            ]  # fmt: skip
        if index == self.close_at:
            return [
                Signal(self.id, bar.venue, bar.symbol, SignalAction.CLOSE, bar.close_time,
                       bar.close)
            ]  # fmt: skip
        return []


def _market_config() -> object:
    """Revolut X con órdenes a mercado (sin post-only) para precios de ejecución exactos."""
    return with_venue(repo_config(prefer_post_only=False), "revolutx")


async def test_no_look_ahead_fills_at_next_bar_open(t0: datetime) -> None:
    # La señal se da al cierre de la vela 1 (100). La vela 2 abre con un hueco a 120:
    # una estrategia que "viera el futuro" compraría a 100; aquí se compra a ~120.
    bars = make_bars([100, 100, 120, 121, 122, 123], t0)
    bars[2] = Bar("revolutx", "BTC/EUR", "5m", bars[2].open_time, 120, 121, 119.5, 120, 1)
    result = await run_backtest(repo_config(prefer_post_only=False), Scripted(1, 4), bars, D(100))
    trade = result.trades[0]
    assert trade.entry_time == bars[2].open_time
    assert trade.entry_price == D("120") * (1 + D("0.0003"))  # medio spread + deslizamiento
    assert trade.exit_time == bars[5].open_time
    assert trade.exit_reason == "señal de salida"


async def test_stop_loss_exits_and_is_reported(t0: datetime) -> None:
    bars = make_bars([100, 100, 100, 90, 89, 88], t0)
    result = await run_backtest(repo_config(prefer_post_only=False), Scripted(1, 99), bars, D(100))
    trade = result.trades[0]
    assert trade.exit_reason == "stop_loss"
    assert trade.pnl < 0
    assert result.open_position is None


async def test_backtest_is_deterministic(t0: datetime) -> None:
    closes = [100 + (i % 17) - (i % 5) * 1.5 + i * 0.05 for i in range(600)]
    params = EmaCrossParams(fast=5, slow=20, atr_period=10, take_profit_atr=2)

    async def once() -> list[tuple[datetime, Decimal]]:
        result = await run_backtest(repo_config(), EmaCross(params), make_bars(closes, t0), D(100))
        return result.equity_curve

    first = await once()
    assert first == await once()


async def test_ema_cross_end_to_end_with_metrics_and_report(t0: datetime, tmp_path: Path) -> None:
    # Onda con tendencia: produce varios cruces de medias.
    import math

    closes = [100 + 8 * math.sin(i / 25) + i * 0.02 for i in range(1500)]
    config = repo_config(max_risk_per_trade_pct=1)
    params = EmaCrossParams(fast=8, slow=21, atr_period=14, take_profit_atr=None)
    result = await run_backtest(config, EmaCross(params), make_bars(closes, t0), D(100))

    assert result.signals > 0
    assert len(result.trades) >= 3
    assert len(result.equity_curve) == 1500
    metrics = compute_metrics(result)
    assert metrics.trades == len(result.trades)
    assert 0 <= metrics.win_rate_pct <= 100
    assert metrics.max_drawdown_pct >= 0
    assert metrics.fees_paid >= 0
    total_pnl = sum((t.pnl for t in result.trades), D(0))
    if result.open_position is None:
        # Identidad contable: capital final = inicial + P&L neto de todas las operaciones.
        assert abs(result.final_equity - (D(100) + total_pnl)) < D("1e-12")

    folder = save_reports(
        tmp_path,
        "ema_cross",
        [MarketReport(result, metrics, data_source="sintético", quote_currency="EUR")],
    )
    assert {p.name for p in folder.iterdir()} == {
        "summary.json", "trades.csv", "equity.csv", "report.html"
    }  # fmt: skip
    html = (folder / "report.html").read_text(encoding="utf-8")
    assert "<svg" in html and "revolutx:BTC/EUR" in html


async def test_rejects_mixed_markets(t0: datetime) -> None:
    bars = make_bars([1, 2], t0) + make_bars([1, 2], t0, symbol="ETH/EUR")
    with pytest.raises(ValueError, match="único"):
        await run_backtest(repo_config(), Scripted(0, 1), bars, D(100))


def test_max_drawdown() -> None:
    assert max_drawdown_pct([D(100), D(120), D(90), D(130), D(117)]) == pytest.approx(25.0)
    assert max_drawdown_pct([D(100), D(101)]) == 0.0
