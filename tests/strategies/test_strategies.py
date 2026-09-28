from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from invertio.core.models import Position, SignalAction
from invertio.strategies import STRATEGIES, load_strategy
from invertio.strategies.ema_cross import EmaCross, EmaCrossParams
from invertio.strategies.rsi_reversion import RsiReversion, RsiReversionParams
from tests.conftest import REPO_ROOT
from tests.helpers import make_bars


class FakeContext:
    def __init__(self) -> None:
        self.open: set[str] = set()

    def position(self, venue: str, symbol: str) -> Position:
        from decimal import Decimal

        return Position(venue, symbol, quantity=Decimal(1) if symbol in self.open else Decimal(0))


def test_ema_cross_buys_on_cross_up_and_closes_on_cross_down(t0: datetime) -> None:
    strategy = EmaCross(EmaCrossParams(fast=3, slow=6, atr_period=3, take_profit_atr=None))
    ctx = FakeContext()
    closes = [100.0] * 8 + [99, 98, 97, 96] + [100, 104, 108] + [104, 99, 94, 90]
    signals = []
    for bar in make_bars(closes, t0):
        for signal in strategy.on_bar(bar, ctx):
            signals.append((bar, signal))
            if signal.action is SignalAction.BUY:
                ctx.open.add(bar.symbol)
            else:
                ctx.open.discard(bar.symbol)

    actions = [s.action for _, s in signals]
    assert actions == [SignalAction.BUY, SignalAction.CLOSE]
    signal_bar, buy = signals[0]
    assert buy.price == signal_bar.close
    assert buy.ts == signal_bar.close_time  # la señal nace al cierre de su vela
    assert buy.stop_loss is not None and buy.stop_loss < buy.price
    assert buy.take_profit is None


def test_ema_cross_is_quiet_during_warmup(t0: datetime) -> None:
    strategy = EmaCross(EmaCrossParams(fast=3, slow=6, atr_period=3))
    ctx = FakeContext()
    rising = [100 + i for i in range(strategy.warmup_bars - 1)]
    assert all(strategy.on_bar(b, ctx) == [] for b in make_bars(rising, t0))


def test_rsi_reversion_respects_trend_filter(t0: datetime) -> None:
    params = RsiReversionParams(rsi_period=3, oversold=30, exit_above=55, atr_period=3, trend_ema=5)
    closes = [100, 102, 104, 106, 108, 110, 112, 114, 108, 104, 101, 116, 120]
    ctx = FakeContext()
    with_filter = [s for b in make_bars(closes, t0) for s in RsiReversion(params).on_bar(b, ctx)]
    no_filter_params = params.model_copy(update={"trend_ema": None})
    strategy = RsiReversion(no_filter_params)
    without_filter = [s for b in make_bars(closes, t0) for s in strategy.on_bar(b, ctx)]
    assert len(without_filter) >= len(with_filter)
    assert all(s.action is SignalAction.BUY for s in without_filter)


def test_params_validation() -> None:
    with pytest.raises(ValidationError, match="rápida"):
        EmaCrossParams(fast=30, slow=10)
    with pytest.raises(ValidationError, match="oversold"):
        RsiReversionParams(oversold=60, exit_above=50)
    with pytest.raises(ValidationError):
        EmaCrossParams.model_validate({"fast": 5, "unknown": 1})


def test_load_strategy_from_repo_config() -> None:
    for strategy_id in STRATEGIES:
        strategy = load_strategy(strategy_id, REPO_ROOT / "config")
        assert strategy.id == strategy_id
    with pytest.raises(ValueError, match="desconocida"):
        load_strategy("nope", Path("."))
