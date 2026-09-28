from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from invertio.core.models import Bar
from invertio.experiments.execution_policies import definitions, evaluate
from invertio.experiments.policies import evaluate as baseline_evaluate
from invertio.indicators import ATR, RSI
from invertio.simulation.config import SimulationConfig

START = datetime(2026, 9, 29, 12, tzinfo=UTC)
IDS = ("rsi_1m_taker", "rsi_1m_maker", "rsi_5m_taker", "rsi_5m_maker")


def history(prices: list[float], *, start: datetime = START) -> list[Bar]:
    return [
        Bar(
            venue="revolutx",
            symbol="BTC/EUR",
            timeframe="1m",
            open_time=start + timedelta(minutes=index),
            open=price,
            high=price + 0.1,
            low=price - 0.1,
            close=price,
            volume=100,
        )
        for index, price in enumerate(prices)
    ]


def test_definitions_isolate_equal_capital_and_declare_cost_and_frequency_assumptions() -> None:
    values = definitions()
    assert [item["id"] for item in values] == list(IDS)
    for item in values:
        config = SimulationConfig.model_validate(item["simulation_config"])
        assert config.initial_eur == Decimal(50)
        assert config.allocation_eur == Decimal(10)
        assert config.max_positions == 5
        assert config.fee_pct == Decimal("0.09")
        assert config.stop_atr == Decimal(2)
        assert config.target_atr == Decimal(3)
        assert item["version"] == "execution-v1"
        assert item["experimental"] is True
        assert item["sources"]
        assert item["warmup_bars"] == 16
        if item["timeframe_minutes"] == 1:
            assert config.lifetime_minutes == 120
            assert item["pending_minutes"] == 5
            assert item["cooldown_minutes"] == 0
        else:
            assert config.lifetime_minutes == 240
            assert item["pending_minutes"] == 15
            assert item["cooldown_minutes"] == 15
        if item["execution"] == "maker_entry":
            text = " ".join(item["rules"])
            assert "0 %" in text and "0,09 %" in text
            assert "sin ejecutar" in text
            assert "estrictamente por debajo" in text
            assert "cola de órdenes" in text
    values[0]["simulation_config"]["initial_eur"] = "1000"
    values[0]["sources"][0]["url"] = "changed"
    assert definitions()[0]["simulation_config"]["initial_eur"] == "50"
    assert definitions()[0]["sources"][0]["url"] != "changed"
    json.dumps(definitions(), allow_nan=False)


@pytest.mark.parametrize("minutes", [1, 5])
def test_both_execution_modes_receive_identical_rsi_cross_signal(minutes: int) -> None:
    closes = [100 - index for index in range(20)] + [91, 115]
    bars = history([price for price in closes for _ in range(minutes)])
    entry_bars = bars[:-minutes]
    taker = evaluate(f"rsi_{minutes}m_taker", entry_bars, entry_bars[-1].close_time)
    maker = evaluate(f"rsi_{minutes}m_maker", entry_bars, entry_bars[-1].close_time)
    assert taker == maker
    assert taker["ready"] and taker["enter"] and not taker["exit"]
    assert taker["indicators"]["previous_rsi14"] <= 30
    assert 30 < taker["indicators"]["rsi14"] < 55
    final = evaluate(f"rsi_{minutes}m_taker", bars, bars[-1].close_time)
    assert final["exit"] is True
    assert final["enter"] is False


def test_one_minute_matches_existing_rsi_strategy_numerically() -> None:
    bars = history([100 - index for index in range(20)] + [91, 115])
    for count in range(16, len(bars) + 1):
        now = bars[count - 1].close_time
        actual = evaluate("rsi_1m_taker", bars[:count], now)
        expected = baseline_evaluate("rsi_rebound", bars[:count], now)
        for key in ("ready", "enter", "exit", "atr", "bar_ts", "entry_reason", "exit_reason"):
            assert actual[key] == expected[key]
        for key in ("rsi14", "previous_rsi14", "close", "volume"):
            assert actual["indicators"][key] == expected["indicators"][key]


@pytest.mark.parametrize("ident", IDS)
def test_future_and_unclosed_bars_never_change_a_decision_or_mutate_inputs(ident: str) -> None:
    bars = history([100 + index / 10 for index in range(100)])
    now = bars[-1].close_time + timedelta(seconds=30)
    before = copy.deepcopy(bars)
    baseline = evaluate(ident, bars, now)
    future = replace(
        bars[-1], open_time=bars[-1].close_time, close=1000, high=2000, volume=10_000
    )
    assert evaluate(ident, [*bars, future], now) == baseline
    assert bars == before
    assert baseline["current_bar_ts"] == bars[-1].close_time.isoformat()
    assert baseline["signal_bar_ts"] == bars[-1].close_time.isoformat()


def test_five_minute_signal_ignores_current_partial_group_even_with_extreme_prices() -> None:
    full = history([100 + index / 10 for index in range(100)])
    partial = history([1, 3000, 1, 3000], start=full[-1].close_time)
    baseline = evaluate("rsi_5m_taker", full, full[-1].close_time)
    actual = evaluate("rsi_5m_taker", [*full, *partial], partial[-1].close_time)
    assert actual["ready"] is True
    for key in ("enter", "exit", "atr", "indicators", "signal_bar_ts", "bar_ts"):
        assert actual[key] == baseline[key]
    assert actual["current_bar_ts"] == partial[-1].close_time.isoformat()


def test_five_minute_aggregation_uses_utc_buckets_and_exact_ohlcv() -> None:
    bars = history([100 + index / 10 for index in range(83)], start=START + timedelta(minutes=2))
    decision = evaluate("rsi_5m_taker", bars, bars[-1].close_time)
    assert decision["ready"] is True
    assert decision["signal_bar_ts"] == (START + timedelta(minutes=85)).isoformat()
    rsi, atr = RSI(14), ATR(14)
    previous, value, expected_atr = None, None, None
    # The initial 12:02–12:05 fragment is excluded, then sixteen full UTC blocks.
    for offset in range(3, 83, 5):
        block = bars[offset : offset + 5]
        previous, value = value, rsi.update(block[-1].close)
        expected_atr = atr.update(
            max(bar.high for bar in block), min(bar.low for bar in block), block[-1].close
        )
    assert decision["atr"] == expected_atr
    assert decision["indicators"]["rsi14"] == value
    assert decision["indicators"]["previous_rsi14"] == previous
    assert decision["indicators"]["volume"] == 500
    assert decision["indicators"]["close"] == bars[-1].close


@pytest.mark.parametrize("minutes", [1, 5])
def test_warmup_requires_sixteen_complete_indicator_bars(minutes: int) -> None:
    bars = history([100 + index / 10 for index in range(16 * minutes)])
    assert evaluate(f"rsi_{minutes}m_taker", bars[:-1], bars[-2].close_time)["ready"] is False
    assert evaluate(f"rsi_{minutes}m_taker", bars, bars[-1].close_time)["ready"] is True


@pytest.mark.parametrize("minutes", [1, 5])
def test_gaps_require_new_consecutive_warmup(minutes: int) -> None:
    bars = history([100 + index / 10 for index in range(100)])
    short_suffix = bars[:90] + bars[91:]
    decision = evaluate(f"rsi_{minutes}m_taker", short_suffix, bars[-1].close_time)
    assert decision["ready"] is False
    assert not decision["enter"] and not decision["exit"]
    enough_suffix = bars[:5] + bars[6:]
    assert evaluate(f"rsi_{minutes}m_taker", enough_suffix, bars[-1].close_time)["ready"] is True


def test_complete_old_five_minute_bar_does_not_override_stale_one_minute_data() -> None:
    bars = history([100 + index / 10 for index in range(84)])
    now = bars[-1].close_time
    assert evaluate("rsi_5m_taker", bars, now)["ready"] is True
    assert evaluate("rsi_5m_taker", bars, now + timedelta(seconds=120))["ready"] is True
    result = evaluate("rsi_5m_taker", bars, now + timedelta(seconds=121))
    assert result["ready"] is False
    assert "un minuto" in result["entry_reason"]


@pytest.mark.parametrize("minutes", [1, 5])
def test_no_volume_prevents_indicator_entry_and_exit(minutes: int) -> None:
    prices = [100 - index for index in range(20)] + [91, 115]
    bars = history([value for value in prices for _ in range(minutes)])
    for index in range(len(bars) - minutes, len(bars)):
        bars[index] = replace(bars[index], volume=0)
    result = evaluate(f"rsi_{minutes}m_taker", bars, bars[-1].close_time)
    assert result["ready"] is True
    assert result["enter"] is False and result["exit"] is False
    assert "sin volumen" in result["entry_reason"]
    entry = bars[:-minutes]
    for index in range(len(entry) - 2 * minutes, len(entry) - minutes):
        entry[index] = replace(entry[index], volume=0)
    assert evaluate(f"rsi_{minutes}m_taker", entry, entry[-1].close_time)["enter"] is False


@pytest.mark.parametrize("ident", ["rsi_1m_taker", "rsi_5m_maker"])
def test_duplicates_mixed_sources_invalid_and_unaligned_bars_are_rejected(ident: str) -> None:
    bars = history([100 + index / 10 for index in range(100)])
    now = bars[-1].close_time
    variants = [
        [*bars, bars[-1]],
        [replace(bars[0], venue="other"), *bars[1:]],
        [replace(bars[0], symbol="ETH/EUR"), *bars[1:]],
        [replace(bar, symbol="BTC/USD") for bar in bars],
        [replace(bar, timeframe="5m") for bar in bars],
        [replace(bars[0], open_time=START + timedelta(seconds=1)), *bars[1:]],
        [*bars[:-1], replace(bars[-1], close=float("nan"))],
        [*bars[:-1], replace(bars[-1], volume=-1)],
        [*bars[:-1], replace(bars[-1], high=bars[-1].close - 0.01)],
    ]
    for invalid in variants:
        result = evaluate(ident, invalid, now)
        assert result["ready"] is False
        assert not result["enter"] and not result["exit"]


def test_empty_unknown_and_naive_now() -> None:
    assert evaluate("rsi_1m_taker", [], START)["ready"] is False
    with pytest.raises(ValueError, match="Estrategia desconocida"):
        evaluate("rsi_rebound", [], START)
    with pytest.raises(ValueError, match="zona horaria"):
        evaluate("rsi_5m_maker", [], START.replace(tzinfo=None))


def test_flat_price_atr_zero_does_not_create_a_signal() -> None:
    bars = [replace(bar, open=100, high=100, low=100, close=100) for bar in history([100] * 100)]
    result = evaluate("rsi_5m_maker", bars, bars[-1].close_time)
    assert result["ready"] is False
    assert result["atr"] == 0
    assert result["enter"] is False and result["exit"] is False


def test_volume_aggregation_overflow_is_invalid_and_json_safe() -> None:
    bars = [replace(bar, volume=1e308) for bar in history([100] * 100)]
    result = evaluate("rsi_5m_taker", bars, bars[-1].close_time)
    assert result["ready"] is False
    assert not result["enter"] and not result["exit"]
    json.dumps(result, allow_nan=False)
