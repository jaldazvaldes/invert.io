import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from invertio.backtest.engine import run_backtest
from invertio.data.store import BarStore
from invertio.lab.config import LabConfig, StrategyGrid, VerdictConfig
from invertio.lab.data import resample
from invertio.lab.report import save_lab
from invertio.lab.runner import Aggregate, Candidate, judge, lab_app_config, run_lab
from invertio.strategies.breakout import BreakoutStrategy
from invertio.strategies.trend import TrendParams, TrendStrategy
from tests.helpers import make_bars, repo_config

T0 = datetime(2025, 1, 1, tzinfo=UTC)


def _lab(**overrides: object) -> LabConfig:
    base: dict[str, object] = {
        "source": "myokx",
        "venue": "revolutx",
        "base_timeframe": "1h",
        "history_days": 90,
        "test_days": 60,
        "min_train_days": 30,
        "min_test_days": 30,
        "strategies": {"tendencia": StrategyGrid(timeframes=["4h"], grid={"ema": [20, 50]})},
    }
    base.update(overrides)
    return LabConfig.model_validate(base)


def test_invalid_combinations_are_skipped() -> None:
    lab = _lab(
        strategies={
            "ruptura": StrategyGrid(
                timeframes=["1h"], grid={"entry_bars": [20, 55], "exit_bars": [10, 50]}
            )
        }
    )
    combos = lab.combinations("ruptura")
    assert {(c["entry_bars"], c["exit_bars"]) for c in combos} == {(20, 10), (55, 10), (55, 50)}


def test_unknown_strategy_rejected() -> None:
    with pytest.raises(ValueError, match="desconocidas"):
        _lab(strategies={"magia": StrategyGrid(timeframes=["1h"])})


def test_resample_hourly_to_4h_keeps_only_complete_buckets() -> None:
    bars = make_bars([float(i) for i in range(1, 12)], T0 + timedelta(hours=1), timeframe="1h")
    four = resample(bars, "4h")
    # 01:00-03:00 queda incompleto (3 velas); 04:00-07:00 y 08:00-11:00 completos (4 cada uno)
    assert [b.open_time.hour for b in four] == [4, 8]
    first = four[0]
    assert (first.open, first.close) == (bars[2].close, bars[6].close)
    assert first.high == max(b.high for b in bars[3:7])
    assert first.timeframe == "4h"
    with pytest.raises(ValueError):
        resample(four, "1h")


async def test_trade_from_warms_up_without_trading() -> None:
    closes = [100 + 10 * math.sin(i / 8) for i in range(400)]
    bars = make_bars(closes, T0, timeframe="1h")
    trade_from = bars[300].close_time
    strategy = BreakoutStrategy(BreakoutStrategy.params_model.model_validate(
        {"entry_bars": 20, "exit_bars": 10}
    ))  # fmt: skip
    result = await run_backtest(repo_config(), strategy, bars, Decimal(100), trade_from=trade_from)
    assert all(t.entry_time >= trade_from for t in result.trades)
    assert result.equity_curve[0][0] > trade_from
    assert result.first_price == bars[301].open


def test_trend_strategy_enters_on_new_uptrend() -> None:
    from decimal import Decimal as D

    from invertio.core.models import Position, SignalAction

    class Ctx:
        open = False

        def position(self, venue: str, symbol: str) -> Position:
            return Position(venue, symbol, quantity=D(1) if self.open else D(0))

    closes = [100 - i * 0.2 for i in range(60)] + [88 + i * 0.5 for i in range(60)]
    closes += [closes[-1] - i * 0.8 for i in range(40)]
    strategy = TrendStrategy(TrendParams(ema=20, slope_bars=3, exit_buffer_atr=0))
    ctx = Ctx()
    actions = []
    for bar in make_bars(closes, T0, timeframe="1h"):
        for signal in strategy.on_bar(bar, ctx):
            actions.append(signal.action)
            ctx.open = signal.action is SignalAction.BUY
    assert actions[:2] == [SignalAction.BUY, SignalAction.CLOSE]


def test_judge_applies_predeclared_rules() -> None:
    lab = _lab(
        verdict=VerdictConfig(min_median_return_pct=0, min_positive_pct=55, min_avg_trades=5)
    )
    good = Aggregate(10, 3.0, 4.0, 70.0, -5.0, 8.0, 12.0, 0)
    bad = Aggregate(10, -2.0, -1.0, 40.0, -5.0, 8.0, 2.0, 0)
    ok = Candidate("tendencia", "4h", {}, 1, train=good, test=good)
    judge(ok, lab)
    assert ok.verdict == "aprueba" and ok.reasons == []
    ko = Candidate("tendencia", "4h", {}, 1, train=good, test=bad)
    judge(ko, lab)
    assert ko.verdict == "no aprueba" and len(ko.reasons) == 3


def test_lab_end_to_end_in_process(tmp_path: Path) -> None:
    store = BarStore(tmp_path / "bars")
    hours = 24 * 130
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for symbol, drift in (("AAA/EUR", 0.0004), ("BBB/EUR", -0.0002)):
        closes = [100 * math.exp(drift * i) * (1 + 0.03 * math.sin(i / 40)) for i in range(hours)]
        store.write("myokx", symbol, "1h", make_bars(closes, start, symbol=symbol, timeframe="1h"))
    lab = _lab()
    config = lab_app_config(
        repo_config(), "revolutx", ["AAA/EUR", "BBB/EUR"], {}, {"AAA/EUR": 0.05}
    )
    now = start + timedelta(hours=hours)
    result = run_lab(lab, config, tmp_path / "bars", workers=0, now=now)

    assert result.symbols_train == ["AAA/EUR", "BBB/EUR"]
    assert result.combinations == 2  # ema 20 y 50 en 4h
    best = [c for c in result.candidates if c.train_rank == 1]
    assert len(best) == 1 and best[0].test is not None
    assert best[0].verdict in {"aprueba", "no aprueba"}
    assert {r["symbol"] for r in best[0].test_rows} == {"AAA/EUR", "BBB/EUR"}
    # El spread real de AAA (0,05 %) se aplica solo a esa moneda.
    venue = config.venue("revolutx")
    assert venue.simulation_for("AAA/EUR").spread_pct == 0.05
    assert venue.simulation_for("BBB/EUR").spread_pct == venue.simulation.spread_pct

    folder = save_lab(tmp_path / "lab", result, lab)
    assert {p.name for p in folder.iterdir()} == {
        "summary.json", "train_all.json", "test_by_symbol.csv", "report.html"
    }  # fmt: skip
