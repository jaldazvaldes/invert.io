import math
from datetime import UTC, datetime
from decimal import Decimal

from invertio.core.models import Bar, Position, Signal, SignalAction
from invertio.strategies import STRATEGIES
from invertio.strategies.exits import Chandelier
from invertio.strategies.pullback import PullbackParams, PullbackStrategy
from invertio.strategies.squeeze import SqueezeParams, SqueezeStrategy
from invertio.strategies.trailing_breakout import TrailingBreakoutParams, TrailingBreakoutStrategy
from tests.helpers import make_bars

T0 = datetime(2026, 1, 1, tzinfo=UTC)


class _Ctx:
    def __init__(self) -> None:
        self.holding = False

    def position(self, venue: str, symbol: str) -> Position:
        return Position(venue, symbol, quantity=Decimal(int(self.holding)))


def run(strategy: object, bars: list[Bar]) -> list[tuple[int, Signal]]:
    """Recorre las velas como el motor: la posición existe tras una compra y hasta una venta."""
    ctx = _Ctx()
    out = []
    for i, bar in enumerate(bars):
        for signal in strategy.on_bar(bar, ctx):  # type: ignore[attr-defined]
            out.append((i, signal))
            ctx.holding = signal.action is SignalAction.BUY
    return out


def test_new_strategies_are_registered() -> None:
    assert {"ruptura_dinamica", "compresion", "retroceso"} <= set(STRATEGIES)


def test_chandelier_follows_the_highest_high() -> None:
    exit_ = Chandelier(bars=3, atr_period=2)
    for bar in make_bars([10, 11, 12, 11], T0, timeframe="1h", wick_pct=0):
        exit_.update(bar)
    assert exit_.highest == 12
    level = exit_.level(2)
    assert level is not None and exit_.atr is not None and level == 12 - 2 * exit_.atr


def test_trailing_breakout_lets_the_trend_run_and_exits_on_reversal() -> None:
    closes = [100.0 + math.sin(i / 3) for i in range(40)]  # lateral
    closes += [102 + 1.5 * i for i in range(30)]  # rompe y sube
    closes += [closes[-1] - 3 * i for i in range(1, 15)]  # se da la vuelta
    strategy = TrailingBreakoutStrategy(TrailingBreakoutParams(entry_bars=20, stop_atr=3))
    signals = run(strategy, make_bars(closes, T0, timeframe="1h"))
    buy_at, buy = signals[0]
    assert buy.action is SignalAction.BUY and 40 <= buy_at < 45
    assert buy.stop_loss is not None and buy.stop_loss < buy.price
    sell_at, sell = signals[1]
    # No vende durante la subida: aguanta hasta que el precio cae bajo el stop que sigue.
    assert sell.action is SignalAction.CLOSE and sell_at >= 70
    assert sell.price > buy.price


def test_squeeze_buys_breakouts_after_calm_but_not_in_chaos() -> None:
    wide = [100.0 + 4 * math.sin(i / 2) for i in range(60)]
    calm = [*wide, *(100.0 + 0.2 * (i % 2) for i in range(30))]
    breakout = [*calm, 103.0, 106.0, 109.0]
    params = SqueezeParams(breakout_bars=20, squeeze_ratio=0.6, squeeze_window=10)
    signals = run(SqueezeStrategy(params), make_bars(breakout, T0, timeframe="1h"))
    assert [s.action for _, s in signals][:1] == [SignalAction.BUY]
    assert signals[0][0] >= 90
    wild = [100.0 + 4 * math.sin(i / 2) for i in range(90)] + [110.0, 114.0]
    assert not [s for _, s in run(SqueezeStrategy(params), make_bars(wild, T0, timeframe="1h"))
                if s.action is SignalAction.BUY]  # fmt: skip


def test_pullback_buys_dips_only_inside_an_uptrend() -> None:
    params = PullbackParams(fast_ema=10, slow_ema=30, dip=20, exit_rsi=70)
    up = [100.0 + 0.8 * i for i in range(60)]
    dip = [*up, up[-1] - 2, up[-1] - 4, up[-1] - 5, up[-1] - 1, up[-1] + 2, up[-1] + 5]
    signals = run(PullbackStrategy(params), make_bars(dip, T0, timeframe="4h"))
    actions = [s.action for _, s in signals]
    assert actions[:2] == [SignalAction.BUY, SignalAction.CLOSE]
    assert signals[0][0] >= 60  # tras la caída, no durante la subida
    down = [200.0 - 0.8 * i for i in range(60)]
    down += [down[-1] - 2, down[-1] - 4, down[-1] - 5, down[-1] - 1, down[-1] + 2]
    assert not run(PullbackStrategy(params), make_bars(down, T0, timeframe="4h"))


def test_signals_never_use_future_candles() -> None:
    closes = [100.0 + 10 * math.sin(i / 7) + i * 0.2 for i in range(300)]
    bars = make_bars(closes, T0, timeframe="1h")
    for cls, params in (
        (TrailingBreakoutStrategy, TrailingBreakoutParams(entry_bars=20)),
        (SqueezeStrategy, SqueezeParams()),
        (PullbackStrategy, PullbackParams()),
    ):
        full = run(cls(params), bars)  # type: ignore[arg-type]
        for cut in (120, 200, 260):
            partial = run(cls(params), bars[:cut])  # type: ignore[arg-type]
            assert [(i, s.action) for i, s in partial] == [
                (i, s.action) for i, s in full if i < cut
            ]
