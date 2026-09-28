"""Lifecycle examples using closed candles; no market/network dependencies."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

import pytest

from invertio.analysis.tracking import HORIZONS, finish, hypothetical_return, track_bars
from invertio.core.models import Bar


def opportunity(created: datetime, **changes: Any) -> dict[str, Any]:
    return {
        "id": "tracking-test",
        "market": "revolutx:BTC/EUR",
        "created_at": created.isoformat(),
        "updated_at": created.isoformat(),
        "last_tracked_at": created.isoformat(),
        "config": {"lifetime_minutes": 240, "taker_fee_pct": 0.09},
        "costs": {"fee_pct": 0.09, "exit_price_factor": 0.999},
        "quantity": 1.0,
        "reference_eur": 100.0,
        "entry": 100.0,
        "stop": 98.0,
        "target": 103.0,
        "status": "open",
        "quality": "complete",
        "outcome": None,
        "ended_at": None,
        "estimated_return_pct": None,
        "end_price": None,
        "high_water": 100.0,
        "low_water": 100.0,
        "mfe_pct": 0.0,
        "mae_pct": 0.0,
        "tracking_gaps": False,
        "horizons": {
            name: {"at": None, "return_pct": None, "net_return_pct": None, "quality": "pending"}
            for name in HORIZONS
        },
        **changes,
    }


def candle(opened: datetime, **changes: float) -> Bar:
    values = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10.0}
    values.update(changes)
    return Bar(venue="revolutx", symbol="BTC/EUR", timeframe="1m", open_time=opened, **values)


def test_hypothetical_return_charges_each_fee_once_and_exit_friction(t0: datetime) -> None:
    item = opportunity(t0)
    expected = (103 * 0.999 * (1 - 0.0009) - 100 * (1 + 0.0009)) / 100 * 100
    assert hypothetical_return(item, 103) == pytest.approx(expected)
    assert hypothetical_return(item, 100) < 0
    item["costs"] = {"fee_pct": 0.0, "exit_price_factor": 1.0}
    assert hypothetical_return(item, 103) == pytest.approx(3.0)


def test_creation_candle_and_unclosed_candle_cannot_determine_outcome(t0: datetime) -> None:
    created = t0 + timedelta(seconds=30)
    item = opportunity(created)
    track_bars(
        item,
        [
            candle(t0, low=90, high=110),
            candle(t0 + timedelta(minutes=1)),
            candle(t0 + timedelta(minutes=2), low=90, high=110),
        ],
        t0 + timedelta(minutes=2, seconds=30),
    )
    assert item["status"] == "open"
    assert item["outcome"] is None
    assert item["high_water"] == 101
    assert item["low_water"] == 99
    assert item["last_tracked_at"] == (t0 + timedelta(minutes=2)).isoformat()


def test_stop_and_target_in_same_candle_have_no_invented_return(t0: datetime) -> None:
    item = opportunity(t0)
    track_bars(item, [candle(t0, low=97, high=104)], t0 + timedelta(minutes=1))
    assert item["status"] == "closed"
    assert item["outcome"] == "ambiguous"
    assert item["quality"] == "ambiguous"
    assert item["estimated_return_pct"] is None
    assert item["end_price"] is None


@pytest.mark.parametrize(
    ("values", "outcome", "price"),
    [
        ({"low": 97.0, "high": 101.0}, "stop", 98.0),
        ({"open": 95.0, "low": 94.0, "high": 97.0, "close": 96.0}, "stop", 95.0),
        ({"low": 99.0, "high": 104.0}, "target", 103.0),
    ],
)
def test_touch_outcomes_and_adverse_open_gap(
    t0: datetime, values: dict[str, float], outcome: str, price: float
) -> None:
    item = opportunity(t0)
    track_bars(item, [candle(t0, **values)], t0 + timedelta(minutes=1))
    assert item["outcome"] == outcome
    assert item["end_price"] == price
    assert item["estimated_return_pct"] == pytest.approx(hypothetical_return(item, price))
    if outcome == "stop":
        assert item["estimated_return_pct"] < 0


def test_first_result_is_frozen_but_horizons_continue_after_it(t0: datetime) -> None:
    item = opportunity(t0)
    bars = [candle(t0, low=97)] + [
        candle(t0 + timedelta(minutes=minute), close=101) for minute in range(1, 15)
    ]
    track_bars(item, bars, t0 + timedelta(minutes=15))
    assert item["outcome"] == "stop"
    assert item["ended_at"] == (t0 + timedelta(minutes=1)).isoformat()
    assert item["horizons"]["15m"]["quality"] == "complete"
    assert item["horizons"]["15m"]["return_pct"] == pytest.approx(1.0)
    snapshot = deepcopy(item)
    track_bars(item, bars, t0 + timedelta(minutes=15))
    assert item == snapshot
    finish(item, "target", t0 + timedelta(minutes=16), "later touch", price=103)
    assert item == snapshot


def test_complete_horizons_and_expiry_use_recorded_prices(t0: datetime) -> None:
    item = opportunity(t0, stop=90.0, target=110.0)
    bars = [candle(t0 + timedelta(minutes=minute), close=101, high=101.5) for minute in range(240)]
    track_bars(item, bars, t0 + timedelta(hours=4))
    assert item["outcome"] == "expired"
    assert item["ended_at"] == (t0 + timedelta(hours=4)).isoformat()
    for label, minutes in HORIZONS.items():
        horizon = item["horizons"][label]
        assert horizon["at"] == (t0 + timedelta(minutes=minutes)).isoformat()
        assert horizon["quality"] == "complete"
        assert horizon["return_pct"] == pytest.approx(1)
        assert horizon["net_return_pct"] == pytest.approx(hypothetical_return(item, 101))
    assert item["mfe_pct"] == pytest.approx(1.5)
    assert item["mae_pct"] == pytest.approx(-1)


def test_a_single_missing_candle_cannot_award_a_complete_target(t0: datetime) -> None:
    item = opportunity(t0 + timedelta(seconds=30))
    track_bars(
        item,
        [
            candle(t0 + timedelta(minutes=1)),
            # The candle opening at minute 2 is absent: its unseen low could hit the stop.
            candle(t0 + timedelta(minutes=3), high=104),
        ],
        t0 + timedelta(minutes=4),
    )
    assert item["status"] == "interrupted"
    assert item["quality"] == "incomplete"
    assert item["tracking_gaps"] is True
    assert item["estimated_return_pct"] is None


def test_no_trade_minute_advances_cursor_without_touch_or_extremes(t0: datetime) -> None:
    item = opportunity(t0)
    track_bars(
        item,
        [candle(t0, open=104, high=104, low=104, close=104, volume=0)],
        t0 + timedelta(minutes=1),
    )
    assert item["status"] == "open"
    assert item["tracking_gaps"] is False
    assert item["last_tracked_at"] == (t0 + timedelta(minutes=1)).isoformat()
    assert item["high_water"] == item["low_water"] == 100
    assert item["mfe_pct"] == item["mae_pct"] == 0
    track_bars(item, [candle(t0 + timedelta(minutes=1))], t0 + timedelta(minutes=2))
    assert item["status"] == "open"
    assert item["tracking_gaps"] is False
    assert item["high_water"] == 101
    assert item["low_water"] == 99


def test_no_trade_horizon_is_incomplete_without_borrowing_a_later_price(t0: datetime) -> None:
    item = opportunity(t0)
    bars = [candle(t0 + timedelta(minutes=minute)) for minute in range(14)]
    bars.append(candle(t0 + timedelta(minutes=14), volume=0))
    track_bars(item, bars, t0 + timedelta(minutes=15))
    horizon = deepcopy(item["horizons"]["15m"])
    assert horizon == {
        "at": (t0 + timedelta(minutes=15)).isoformat(),
        "return_pct": None,
        "net_return_pct": None,
        "quality": "incomplete",
    }
    assert item["status"] == "open"
    assert item["tracking_gaps"] is False
    track_bars(
        item,
        [candle(t0 + timedelta(minutes=minute)) for minute in range(15, 60)],
        t0 + timedelta(hours=1),
    )
    assert item["horizons"]["15m"] == horizon
    assert item["horizons"]["1h"]["quality"] == "complete"


@pytest.mark.parametrize("offset_seconds", [0, 30])
def test_expiry_during_no_trade_minute_has_no_synthetic_exit_price(
    t0: datetime, offset_seconds: int
) -> None:
    item = opportunity(
        t0 + timedelta(seconds=offset_seconds),
        config={"lifetime_minutes": 1, "taker_fee_pct": 0.09},
    )
    opened = t0 + timedelta(minutes=1 if offset_seconds else 0)
    track_bars(item, [candle(opened, volume=0)], opened + timedelta(minutes=1))
    assert item["outcome"] == "expired"
    assert item["ended_at"] == (t0 + timedelta(minutes=1, seconds=offset_seconds)).isoformat()
    assert item["quality"] == "incomplete"
    assert item["end_price"] is None
    assert item["estimated_return_pct"] is None
    assert item["tracking_gaps"] is False


def test_expiry_inside_candle_cannot_borrow_its_later_close(t0: datetime) -> None:
    item = opportunity(
        t0 + timedelta(seconds=30),
        config={"lifetime_minutes": 1, "taker_fee_pct": 0.09},
    )
    track_bars(item, [candle(t0 + timedelta(minutes=1))], t0 + timedelta(minutes=2))
    assert item["outcome"] == "expired"
    assert item["ended_at"] == (t0 + timedelta(minutes=1, seconds=30)).isoformat()
    assert item["quality"] == "incomplete"
    assert item["estimated_return_pct"] is None


def test_level_touch_and_expiry_inside_same_candle_are_ambiguous(t0: datetime) -> None:
    item = opportunity(
        t0 + timedelta(seconds=30),
        config={"lifetime_minutes": 1, "taker_fee_pct": 0.09},
    )
    track_bars(item, [candle(t0 + timedelta(minutes=1), high=104)], t0 + timedelta(minutes=2))
    assert item["outcome"] == "ambiguous"
    assert item["quality"] == "ambiguous"
    assert item["estimated_return_pct"] is None


def test_missing_horizon_data_is_incomplete_and_still_recoverable(t0: datetime) -> None:
    item = opportunity(t0)
    track_bars(item, [], t0 + timedelta(minutes=18))
    assert item["horizons"]["15m"]["quality"] == "incomplete"
    assert item["horizons"]["15m"]["at"] is None
    bars = [candle(t0 + timedelta(minutes=minute)) for minute in range(15)]
    track_bars(item, bars, t0 + timedelta(minutes=18))
    assert item["horizons"]["15m"]["quality"] == "complete"
    assert item["horizons"]["15m"]["at"] == (t0 + timedelta(minutes=15)).isoformat()


def test_extremes_stop_accumulating_after_four_hour_window(t0: datetime) -> None:
    item = opportunity(t0, config={"lifetime_minutes": 480, "taker_fee_pct": 0.09})
    bars = [candle(t0 + timedelta(minutes=minute)) for minute in range(240)]
    bars.append(candle(t0 + timedelta(hours=4), low=50, high=200))
    track_bars(item, bars, t0 + timedelta(hours=4, minutes=1))
    assert item["high_water"] == 101
    assert item["low_water"] == 99
