import math
from datetime import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from invertio.core.models import Bar, Position, SignalAction
from invertio.indicators import MACD, ROC, RollingMax
from invertio.strategies.score import FACTORS, ScoreParams, ScoreStrategy, ScoreWeights
from tests.helpers import make_bars

SMALL = {"ema_fast": 5, "ema_trend": 20, "roc_bars": 3, "breakout_bars": 10, "volume_period": 5}


class Ctx:
    def __init__(self) -> None:
        self.open = False

    def position(self, venue: str, symbol: str) -> Position:
        return Position(venue, symbol, quantity=Decimal(1) if self.open else Decimal(0))


def _scores(closes: list[float], t0: datetime, **params: object) -> list[float]:
    strategy = ScoreStrategy(ScoreParams(**{**SMALL, "min_atr_pct": 0, **params}))
    results = [strategy.evaluate(b) for b in make_bars(closes, t0)]
    return [r.score for r in results if r is not None]


def test_new_indicators() -> None:
    roc = ROC(2)
    assert [roc.update(x) for x in (100, 105, 110)] == [None, None, pytest.approx(10.0)]
    high = RollingMax(3)
    assert [high.update(x) for x in (1, 5, 2, 3, 1)] == [None, None, 5, 5, 3]
    macd = MACD(2, 4, 2)
    values = [macd.update(float(x)) for x in range(1, 12)]
    assert values[-1] is not None and macd.line is not None and macd.line > 0


def test_uptrend_scores_high_and_downtrend_low(t0: datetime) -> None:
    up = [100 * (1.003**i) + math.sin(i) * 0.05 for i in range(80)]
    down = [100 * (0.997**i) + math.sin(i) * 0.05 for i in range(80)]
    up_scores, down_scores = _scores(up, t0), _scores(down, t0)
    assert up_scores and down_scores
    assert up_scores[-1] >= 60
    assert down_scores[-1] <= 20
    assert all(0 <= s <= 100 for s in up_scores + down_scores)


def test_breakdown_adds_up(t0: datetime) -> None:
    strategy = ScoreStrategy(ScoreParams(**SMALL))
    result = None
    for bar in make_bars([100 + i * 0.5 for i in range(60)], t0):
        result = strategy.evaluate(bar) or result
    assert result is not None
    assert result.score == pytest.approx(sum(result.points.values()))
    assert set(result.points) == set(FACTORS)
    assert all(0 <= result.points[f] <= result.max_points[f] for f in FACTORS)
    assert "tendencia" in result.breakdown()
    assert result.stop_loss < result.close
    assert strategy.last_result("revolutx", "BTC/EUR") == result


def test_buys_only_on_threshold_cross_and_exits_on_low_score(t0: datetime) -> None:
    flat = [100 + math.sin(i / 2) * 0.3 for i in range(40)]
    rally = [flat[-1] * (1.004**i) for i in range(1, 25)]
    crash = [rally[-1] * (0.994**i) for i in range(1, 25)]
    strategy = ScoreStrategy(ScoreParams(**{**SMALL, "min_atr_pct": 0}))
    ctx = Ctx()
    actions = []
    for bar in make_bars(flat + rally + crash, t0):
        for signal in strategy.on_bar(bar, ctx):
            actions.append(signal.action)
            ctx.open = signal.action is SignalAction.BUY
            if signal.action is SignalAction.BUY:
                assert "puntuación" in signal.reason and signal.stop_loss is not None
    assert actions == [SignalAction.BUY, SignalAction.CLOSE]


def test_cost_filter_blocks_entries_in_quiet_markets(t0: datetime) -> None:
    flat = [100 + math.sin(i / 2) * 0.3 for i in range(40)]
    rally = [flat[-1] * (1.004**i) for i in range(1, 25)]
    strategy = ScoreStrategy(ScoreParams(**{**SMALL, "min_atr_pct": 5.0}))
    signals = [s for b in make_bars(flat + rally, t0) for s in strategy.on_bar(b, Ctx())]
    assert signals == []
    last = strategy.last_result("revolutx", "BTC/EUR")
    assert last is not None and not last.tradable


def test_scores_ignore_the_current_bar_in_breakout_reference(t0: datetime) -> None:
    """La ruptura compara con el máximo de las velas ANTERIORES: si no, nunca se cumpliría."""
    closes = [100.0] * 50 + [101.0]  # el MACD (12, 26, 9) necesita 34 velas
    strategy = ScoreStrategy(ScoreParams(**SMALL))
    bars: list[Bar] = make_bars(closes, t0, wick_pct=0.0)
    result = None
    for bar in bars:
        result = strategy.evaluate(bar) or result
    assert result is not None and result.points["ruptura"] == 15


def test_weights_must_sum_100() -> None:
    with pytest.raises(ValidationError, match="sumar 100"):
        ScoreWeights(tendencia=50)
    with pytest.raises(ValidationError, match="exit_score"):
        ScoreParams(entry_score=40, exit_score=60)
