from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from invertio.core.models import Bar
from invertio.experiments.policies import definitions, evaluate
from invertio.simulation.config import SimulationConfig
from invertio.strategies.score import ScoreParams, ScoreStrategy

START = datetime(2026, 9, 28, 12, tzinfo=UTC)
STRATEGIES = ("score_base", "trend_ema", "breakout", "rsi_rebound")


def history(prices: list[float], volume: float = 100) -> list[Bar]:
    return [
        Bar(
            venue="revolutx",
            symbol="BTC/EUR",
            timeframe="1m",
            open_time=START + timedelta(minutes=index),
            open=price,
            high=price + 0.1,
            low=price - 0.1,
            close=price,
            volume=volume,
        )
        for index, price in enumerate(prices)
    ]


def test_definitions_are_independent_equal_budget_and_explicit_rules() -> None:
    values = definitions()
    assert [value["id"] for value in values] == [*STRATEGIES, "cash"]
    for value in values:
        config = SimulationConfig.model_validate(value["simulation_config"])
        assert config.initial_eur == Decimal(50)
        assert config.allocation_eur == Decimal(10)
        assert config.max_positions == 5
        assert config.fee_pct == Decimal("0.09")
        assert value["experimental"] is True
        assert value["version"] == "prospective-v1"
        assert value["rules"]
        if value["id"] != "cash":
            assert value["sources"]
            assert value["warmup_bars"] > 0
    assert values[-2]["simulation_config"]["lifetime_minutes"] == 120
    assert values[1]["simulation_config"]["target_atr"] == "4"
    values[0]["simulation_config"]["initial_eur"] = "500"
    values[0]["sources"][0]["url"] = "modified"
    assert definitions()[0]["simulation_config"]["initial_eur"] == "50"
    assert definitions()[0]["sources"][0]["url"] != "modified"
    json.dumps(definitions(), allow_nan=False)


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_open_and_future_candles_cannot_change_the_decision(strategy: str) -> None:
    bars = history([100 + index / 10 for index in range(250)])
    now = bars[-1].close_time + timedelta(seconds=30)
    before = copy.deepcopy(bars)
    baseline = evaluate(strategy, bars, now)
    future = replace(bars[-1], open_time=bars[-1].close_time, close=1000, high=2000, volume=10_000)
    assert evaluate(strategy, [*bars, future], now) == baseline
    assert evaluate(strategy, [*bars, future], future.close_time)["bar_ts"] != baseline["bar_ts"]
    assert bars == before
    assert baseline["bar_ts"] == bars[-1].close_time.isoformat()


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_zero_volume_latest_candle_never_orders_entry_or_indicator_exit(strategy: str) -> None:
    bars = history([100 + index / 10 for index in range(250)])
    bars[-1] = replace(bars[-1], volume=0)
    decision = evaluate(strategy, bars, bars[-1].close_time)
    assert decision["ready"] is True
    assert decision["enter"] is False
    assert decision["exit"] is False
    assert "sin volumen" in decision["entry_reason"]


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_declared_warmup_matches_readiness(strategy: str) -> None:
    count = next(item["warmup_bars"] for item in definitions() if item["id"] == strategy)
    bars = history([100 + index / 10 for index in range(count)])
    assert evaluate(strategy, bars[:-1], bars[-2].close_time)["ready"] is False
    assert evaluate(strategy, bars, bars[-1].close_time)["ready"] is True


def test_score_policy_uses_actual_rule_points_and_thresholds() -> None:
    bars = history([100 + index / 10 for index in range(250)])
    engine = ScoreStrategy(ScoreParams())
    expected = None
    for bar in bars:
        expected = engine.evaluate(bar)
    assert expected is not None
    decision = evaluate("score_base", bars, bars[-1].close_time)
    assert decision["indicators"]["score"] == expected.score
    assert decision["enter"] is (expected.score >= 70)
    assert decision["exit"] is (expected.score <= 40)
    falling = history([200 - index / 10 for index in range(250)])
    falling_result = evaluate("score_base", falling, falling[-1].close_time)
    assert falling_result["indicators"]["score"] <= 40
    assert falling_result["exit"] is True


def test_ema_requires_a_new_cross_and_positive_macd() -> None:
    bars = history([120 - index / 5 for index in range(80)] + [104 + index for index in range(40)])
    entries = []
    for count in range(51, len(bars) + 1):
        decision = evaluate("trend_ema", bars[:count], bars[count - 1].close_time)
        if decision["enter"]:
            entries.append(count)
            values = decision["indicators"]
            assert values["previous_ema20"] <= values["previous_ema50"]
            assert values["ema20"] > values["ema50"]
            assert values["macd_histogram"] > 0
    assert len(entries) == 1
    end = entries[0]
    assert evaluate("trend_ema", bars[: end + 1], bars[end].close_time)["enter"] is False
    assert evaluate("trend_ema", bars[:70], bars[69].close_time)["exit"] is True
    bars[end - 2] = replace(bars[end - 2], volume=0)
    assert evaluate("trend_ema", bars[:end], bars[end - 1].close_time)["enter"] is False


def test_breakout_uses_prior_high_and_volume_excluding_current_candle() -> None:
    bars = history([100 + index / 10 for index in range(60)])
    bars[-1] = replace(bars[-1], high=200, volume=150)
    decision = evaluate("breakout", bars, bars[-1].close_time)
    assert decision["enter"] is True
    assert decision["indicators"]["previous_high20"] == bars[-2].high
    assert decision["indicators"]["previous_volume20"] == 100
    bars[-1] = replace(bars[-1], volume=149.9)
    assert evaluate("breakout", bars, bars[-1].close_time)["enter"] is False
    bars[-1] = replace(bars[-1], volume=150, close=bars[-2].high)
    assert evaluate("breakout", bars, bars[-1].close_time)["enter"] is False


def test_breakout_requires_volume_baseline_and_rising_ema() -> None:
    bars = history([100 + index / 10 for index in range(60)], volume=0)
    bars[-1] = replace(bars[-1], volume=100)
    assert evaluate("breakout", bars, bars[-1].close_time)["enter"] is False
    falling = history([200 - index for index in range(60)])
    assert evaluate("breakout", falling, falling[-1].close_time)["exit"] is True


def test_rsi_rebound_crosses_30_and_exits_at_55() -> None:
    bars = history([100 - index for index in range(20)] + [91, 115])
    decision = evaluate("rsi_rebound", bars[:-1], bars[-2].close_time)
    assert decision["enter"] is True
    assert decision["exit"] is False
    assert decision["indicators"]["previous_rsi14"] <= 30
    assert 30 < decision["indicators"]["rsi14"] < 55
    final = evaluate("rsi_rebound", bars, bars[-1].close_time)
    assert final["enter"] is False
    assert final["exit"] is True
    bars[-3] = replace(bars[-3], volume=0)
    assert evaluate("rsi_rebound", bars[:-1], bars[-2].close_time)["enter"] is False


@pytest.mark.parametrize("strategy", STRATEGIES[1:])
def test_non_score_strategies_do_not_publish_artificial_scores(strategy: str) -> None:
    bars = history([100 + index / 10 for index in range(250)])
    decision = evaluate(strategy, bars, bars[-1].close_time)
    assert "score" not in decision["indicators"]
    json.dumps(decision, allow_nan=False)


def test_stale_gapped_mixed_and_duplicate_data_do_not_make_signals() -> None:
    bars = history([100 + index / 10 for index in range(250)])
    now = bars[-1].close_time
    assert evaluate("score_base", bars, now + timedelta(seconds=121))["ready"] is False
    assert evaluate("score_base", bars, now + timedelta(seconds=120))["ready"] is True
    assert evaluate("score_base", bars[:100] + bars[101:], now)["ready"] is False
    assert evaluate("score_base", [*bars, bars[-1]], now)["ready"] is False
    assert (
        evaluate("score_base", [replace(bars[0], venue="other"), *bars[1:]], now)["ready"] is False
    )
    assert (
        evaluate("score_base", [replace(bar, timeframe="5m") for bar in bars], now)["ready"]
        is False
    )
    assert (
        evaluate("score_base", [replace(bar, symbol="BTC/USD") for bar in bars], now)["ready"]
        is False
    )
    assert (
        evaluate("score_base", [*bars[:-1], replace(bars[-1], volume=float("nan"))], now)["ready"]
        is False
    )


def test_no_history_or_unknown_policy() -> None:
    assert evaluate("score_base", [], START)["ready"] is False
    for strategy in ("unknown", "cash"):
        with pytest.raises(ValueError, match="Estrategia desconocida"):
            evaluate(strategy, [], START)
    with pytest.raises(ValueError, match="zona horaria"):
        evaluate("score_base", [], START.replace(tzinfo=None))
