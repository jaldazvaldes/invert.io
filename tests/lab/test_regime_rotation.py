import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from invertio.core.models import Bar, Position, Signal, SignalAction
from invertio.data.store import BarStore
from invertio.lab.config import LabConfig, RotationConfig, StrategyGrid, split_filter
from invertio.lab.report import save_rotation
from invertio.lab.rotation import RotationRun, daily_series, judge, run_rotation, simulate
from invertio.lab.runner import lab_app_config, run_lab
from invertio.strategies.base import Strategy, StrategyContext, StrategyParams
from invertio.strategies.regime import RegimeFiltered, btc_regime
from tests.helpers import make_bars, repo_config

D0 = datetime(2026, 1, 5, tzinfo=UTC)  # lunes
DAY = timedelta(days=1)


def daily(closes: list[float], symbol: str = "BTC/EUR", start: datetime = D0) -> list[Bar]:
    return [
        Bar("myokx", symbol, "1d", start + i * DAY, close, close * 1.01, close * 0.99, close, 1)
        for i, close in enumerate(closes)
    ]


def test_btc_regime_uses_only_closed_days() -> None:
    bars = daily([10, 11, 12, 13, 8, 7])
    allowed = btc_regime(bars, 3)
    # Día 2 (12 > media 11) cierra en D0 + 3 días: antes de ese instante aún no se conoce.
    assert not allowed(D0 + 3 * DAY - timedelta(seconds=1))
    assert allowed(D0 + 3 * DAY)
    assert allowed(D0 + 4 * DAY + timedelta(hours=5))  # día 3: 13 > 12
    assert not allowed(D0 + 5 * DAY)  # día 4: 8 < media (12+13+8)/3
    assert not allowed(D0)  # sin historia suficiente: sin compras


class _Both(Strategy[StrategyParams]):
    id = "both"
    params_model = StrategyParams

    def __init__(self) -> None:
        super().__init__(StrategyParams())
        self.seen = 0

    @property
    def warmup_bars(self) -> int:
        return 7

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[Signal]:
        self.seen += 1
        return [
            Signal("both", "revolutx", bar.symbol, SignalAction.BUY, bar.close_time, bar.close,
                   stop_loss=bar.close * 0.9),
            Signal("both", "revolutx", bar.symbol, SignalAction.CLOSE, bar.close_time, bar.close),
        ]  # fmt: skip


class _Ctx:
    def position(self, venue: str, symbol: str) -> Position:
        return Position(venue, symbol)


def test_filter_drops_only_buys_and_keeps_indicators_running() -> None:
    inner = _Both()
    state = {"on": False}
    wrapped = RegimeFiltered(inner, lambda at: state["on"])
    bar = daily([100])[0]
    assert [s.action for s in wrapped.on_bar(bar, _Ctx())] == [SignalAction.CLOSE]
    state["on"] = True
    assert len(wrapped.on_bar(bar, _Ctx())) == 2
    assert inner.seen == 2 and wrapped.warmup_bars == 7


def test_filter_values_become_separate_combinations() -> None:
    lab = LabConfig.model_validate({
        "source": "myokx", "venue": "revolutx",
        "strategies": {"tendencia": {"timeframes": ["4h"], "grid": {"ema": [20]},
                                     "btc_filter": [None, 50]}},
    })  # fmt: skip
    combos = lab.combinations("tendencia")
    assert combos == [{"ema": 20, "btc_filter": None}, {"ema": 20, "btc_filter": 50}]
    assert split_filter(combos[1]) == ({"ema": 20}, 50)
    plain = StrategyGrid(timeframes=["4h"], grid={"ema": [20]})
    assert plain.btc_filter == [None]


def test_lab_runs_filtered_and_unfiltered_side_by_side(tmp_path: Path) -> None:
    store = BarStore(tmp_path / "bars")
    hours = 24 * 130
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for symbol, drift in (("AAA/EUR", 0.0004), ("BTC/EUR", -0.0002)):
        closes = [100 * math.exp(drift * i) * (1 + 0.03 * math.sin(i / 40)) for i in range(hours)]
        store.write("myokx", symbol, "1h", make_bars(closes, start, symbol=symbol, timeframe="1h"))
    lab = LabConfig.model_validate({
        "source": "myokx", "venue": "revolutx", "history_days": 90, "test_days": 60,
        "min_train_days": 30, "min_test_days": 30,
        "strategies": {"tendencia": {"timeframes": ["4h"], "grid": {"ema": [20]},
                                     "btc_filter": [None, 5]}},
    })  # fmt: skip
    config = lab_app_config(repo_config(), "revolutx", ["AAA/EUR", "BTC/EUR"], {}, {})
    result = run_lab(lab, config, tmp_path / "bars", workers=0, now=start + timedelta(hours=hours))
    best = [c for c in result.candidates if c.train_rank == 1]
    assert sorted(str(c.params["btc_filter"]) for c in best) == ["5", "None"]
    filtered = next(c for c in best if c.params["btc_filter"] == 5)
    assert filtered.test is not None and filtered.test.symbols == 2
    assert all("error" not in row for row in filtered.test_rows)


def _series(
    growth: float, days: int = 60, start: float = 100
) -> dict[datetime, tuple[float, float]]:
    return {
        D0 + i * DAY: (start * (1 + growth) ** i, start * (1 + growth) ** (i + 1))
        for i in range(days)
    }


def test_rotation_holds_the_strongest_and_pays_costs() -> None:
    data = {"UP/EUR": _series(0.02), "FLAT/EUR": _series(0.0), "DOWN/EUR": _series(-0.02),
            "BTC/EUR": _series(0.001)}  # fmt: skip
    params = {"lookback_days": 7, "top": 1, "btc_filter": None}
    run = simulate(data, params, D0, D0 + 60 * DAY, costs={"UP/EUR": 0.05}, fee_pct=0.09,
                   rebalance_days=7, regime=None, period="test")  # fmt: skip
    assert run.log[0]["bought"] == ["UP/EUR"]
    # Primer lunes con 7 días de historia cerrada antes de la señal: el tercero.
    assert run.log[0]["day"] == (D0 + 14 * DAY).date().isoformat()
    assert run.trades == 1  # sigue siendo la más fuerte: no se reequilibra ni se paga más
    assert run.costs_eur > 0
    ideal = 1.02 ** (60 - 14) * 100
    assert run.equity[-1][1] < ideal and run.return_pct > 0
    assert round(run.btc_return_pct, 6) == round((1.001**60 - 1) * 100, 6)
    # Filtro de BTC apagado: todo en euros, sin operaciones.
    cash = simulate(data, params, D0, D0 + 60 * DAY, costs={}, fee_pct=0.09, rebalance_days=7,
                    regime=lambda at: False, period="test")  # fmt: skip
    assert cash.trades == 0 and cash.return_pct == 0 and cash.exposure_pct == 0


def test_rotation_sells_what_drops_out_and_skips_negative_momentum() -> None:
    first = {D0 + i * DAY: (100.0 + i, 101.0 + i) for i in range(20)}
    first |= {D0 + i * DAY: (120.0 - 3 * (i - 20), 117.0 - 3 * (i - 20)) for i in range(20, 40)}
    second = _series(0.005, 40)
    params = {"lookback_days": 7, "top": 1, "btc_filter": None}
    run = simulate({"A/EUR": first, "B/EUR": second}, params, D0, D0 + 40 * DAY, costs={},
                   fee_pct=0.0, rebalance_days=7, regime=None, period="test")  # fmt: skip
    changes = [(entry["sold"], entry["bought"]) for entry in run.log]
    assert changes[0] == ([], ["A/EUR"])
    assert (["A/EUR"], ["B/EUR"]) in changes
    falling = {D0 + i * DAY: (100.0 - i, 99.0 - i) for i in range(30)}
    none = simulate({"A/EUR": falling}, params, D0, D0 + 30 * DAY, costs={}, fee_pct=0.0,
                    rebalance_days=7, regime=None, period="test")  # fmt: skip
    assert none.trades == 0


def test_rotation_verdict_and_selection(tmp_path: Path) -> None:
    config = RotationConfig(lookback_days=[7, 14], top=[1], btc_filter=[None, 3])
    assert len(config.combinations()) == 4
    data = {"UP/EUR": _series(0.01, 120), "DOWN/EUR": _series(-0.01, 120)}
    btc = daily([100 * 1.002**i for i in range(120)])
    data["BTC/EUR"] = daily_series(btc)
    result = run_rotation(config, data, btc, costs={}, test_start=D0 + 80 * DAY,
                          end=D0 + 120 * DAY)  # fmt: skip
    assert len(result.train) == 4 and len(result.candidates) == 3
    best = result.candidates[0]
    assert best.train.return_pct >= result.train[-1].return_pct
    assert best.test.return_pct > best.test.btc_return_pct
    # Compra UP una vez y la mantiene: gana, pero con una operación no hay muestra.
    assert best.test.trades == 1
    assert best.reasons == ["pocas operaciones en test (1)"] and best.verdict == "no aprueba"
    losing = RotationRun(params={}, period="test", start=D0, end=D0, return_pct=-1,
                         btc_return_pct=5, trades=30)  # fmt: skip
    reasons = judge(best.train, losing, config)
    assert any("test" in r for r in reasons) and any("BTC" in r for r in reasons)
    folder = save_rotation(tmp_path, result, config)
    saved: dict[str, Any] = json.loads((folder / "summary.json").read_text("utf-8"))
    assert saved["candidates"][0]["test"]["equity"]
