from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from invertio.experiments.maker import MakerSimulationEngine
from invertio.simulation.config import SimulationConfig

T0 = datetime(2026, 9, 29, 0, 0, tzinfo=UTC)
D = Decimal


def opportunity(ident: str = "one", symbol: str = "BTC/EUR", **extra: Any) -> dict[str, Any]:
    return {
        "id": ident,
        "market": f"revolutx:{symbol}",
        "created_at": T0.isoformat(),
        "rule_version": "test-v1",
        "status": "open",
        **extra,
    }


def row(at: datetime = T0, symbol: str = "BTC/EUR", **extra: Any) -> dict[str, Any]:
    return {
        "market": f"revolutx:{symbol}",
        "state": "eligible",
        "score": 100,
        "atr": 1,
        "quote_ts": at.isoformat(),
        "bar_ts": at.isoformat(),
        "reasons": [],
        **extra,
    }


def book(
    at: datetime = T0, bid: str = "99.99", ask: str = "100.01", quantity: str = "10"
) -> dict[str, Any]:
    return {"ts": at.isoformat(), "bids": [[bid, quantity]], "asks": [[ask, quantity]]}


def market() -> dict[str, Any]:
    return {
        "active": True,
        "spot": True,
        "quote": "EUR",
        "precision": {"amount": 0.00001, "price": 0.01},
        "limits": {"amount": {"min": 0.00001, "max": 1000}, "cost": {"min": 1}},
    }


def place(engine: MakerSimulationEngine) -> dict[str, Any]:
    return engine.step([row()], [opportunity()], {"BTC/EUR": book()}, {"BTC/EUR": market()}, T0)


def tick(
    engine: MakerSimulationEngine,
    at: datetime,
    *,
    bid: str = "99.97",
    ask: str = "99.98",
    **row_extra: Any,
) -> dict[str, Any]:
    return engine.step(
        [row(at, **row_extra)],
        [opportunity()],
        {"BTC/EUR": book(at, bid, ask)},
        {"BTC/EUR": market()},
        at,
    )


def test_placement_reserves_without_buying_or_reducing_equity() -> None:
    engine = MakerSimulationEngine(SimulationConfig(), None, T0)
    result = place(engine)
    pending = result["pending_orders"]["BTC/EUR"]
    assert pending["status"] == "pending"
    assert pending["limit_price"] == "99.99"
    assert D(pending["quantity"]) % D("0.00001") == 0
    assert pending["stop"] == "97.99" and pending["target"] == "102.99"
    assert result["positions"] == {} and result["maker_orders"] == []
    assert result["cash_eur"] == "40"
    assert result["stats"]["reserved_eur"] == "10"
    assert result["stats"]["equity_eur"] == "50"
    assert result["stats"]["return_pct"] == "0"
    assert result["equity"][-1]["equity_eur"] == "50"
    assert engine.holding_symbols() == ["BTC/EUR"]
    assert [item["action"] for item in result["decisions"]] == ["place"]
    json.dumps(result, allow_nan=False)


def test_strict_later_trade_through_uses_original_limit_and_refunds_rounding_dust() -> None:
    engine = MakerSimulationEngine(SimulationConfig(), None, T0)
    pending = copy.deepcopy(place(engine)["pending_orders"]["BTC/EUR"])
    later = T0 + timedelta(minutes=1)
    result = tick(engine, later, score=50, atr=7)
    trade = result["positions"]["BTC/EUR"]
    assert trade["entry"] == "99.99" and trade["entry_fee_eur"] == "0"
    assert trade["opened_at"] == later.isoformat()
    assert trade["source_created_at"] == T0.isoformat()
    assert trade["execution_model"] == "maker_model_trade_through"
    assert trade["entry_execution_model"] == "maker_model_trade_through"
    assert (trade["stop"], trade["target"]) == (pending["stop"], pending["target"])
    assert D(result["cash_eur"]) == 50 - D(trade["investment_eur"])
    assert not result["pending_orders"]
    assert result["maker_orders"][0]["status"] == "filled"
    filled = result["maker_orders"][0]
    assert filled["placed_best_bid"] == "99.99" and filled["placed_best_ask"] == "100.01"
    assert filled["fill_best_bid"] == "99.97" and filled["fill_best_ask"] == "99.98"
    assert D(filled["fill_depth_below_limit"]) >= D(filled["quantity"])
    assert result["stats"]["maker_fills"] == 1
    assert D(result["stats"]["equity_eur"]) == D(result["cash_eur"]) + D(trade["quantity"]) * D(
        "99.97"
    ) * D("0.9991")


@pytest.mark.parametrize(
    "kind", ["touch", "same_timestamp", "same_cycle", "shallow_asks", "low_only_in_candle"]
)
def test_touch_same_observation_and_inadequate_full_depth_do_not_fill(kind: str) -> None:
    engine = MakerSimulationEngine(SimulationConfig(), None, T0)
    original = copy.deepcopy(place(engine))
    later = T0 + timedelta(seconds=30)
    current = book(later, "99.97", "99.98")
    extra: dict[str, Any] = {}
    if kind == "touch":
        current["asks"] = [["99.99", "100"]]
    elif kind == "same_timestamp":
        current["ts"] = T0.isoformat()
    elif kind == "same_cycle":
        later = T0
        current["ts"] = T0.isoformat()
    elif kind == "shallow_asks":
        current["asks"] = [["99.98", "0.00001"], ["99.99", "100"]]
    else:
        current = book(later)
        extra = {"low": 1, "high": 1000}
    result = engine.step(
        [row(later, **extra)], [opportunity()], {"BTC/EUR": current}, {"BTC/EUR": market()}, later
    )
    assert not result["positions"] and result["pending_orders"] == original["pending_orders"]
    assert result["cash_eur"] == "40" and result["stats"]["equity_eur"] == "50"


def test_complete_depth_can_span_multiple_strictly_lower_levels() -> None:
    engine = MakerSimulationEngine(SimulationConfig(), None, T0)
    place(engine)
    later = T0 + timedelta(minutes=1)
    current = book(later, "99.96", "99.97")
    current["asks"] = [["99.97", "0.05"], ["99.98", "0.06"]]
    result = engine.step(
        [row(later)], [opportunity()], {"BTC/EUR": current}, {"BTC/EUR": market()}, later
    )
    assert result["positions"]["BTC/EUR"]["entry"] == "99.99"


def test_expiry_precedes_a_possible_fill_and_refunds_exactly_once() -> None:
    engine = MakerSimulationEngine(SimulationConfig(), None, T0)
    place(engine)
    for minute in range(1, 5):
        tick(engine, T0 + timedelta(minutes=minute), bid="99.99", ask="100.01", score=50)
    later = T0 + timedelta(minutes=5)
    result = tick(engine, later)
    assert not result["pending_orders"] and not result["positions"]
    assert result["cash_eur"] == "50" and result["stats"]["equity_eur"] == "50"
    assert result["maker_orders"][0]["status"] == "expired"
    assert result["stats"]["maker_expired"] == 1
    saved = copy.deepcopy(result)
    restarted = MakerSimulationEngine(SimulationConfig(), saved, later)
    repeated = tick(restarted, later)
    assert repeated["cash_eur"] == "50"
    assert repeated["maker_orders"] == saved["maker_orders"]
    assert len(repeated["equity"]) == len(saved["equity"])


@pytest.mark.parametrize(
    "kind",
    [
        "gap",
        "stale_quote",
        "future_quote",
        "stale_book",
        "future_book",
        "missing_row",
        "missing_book",
        "interrupted",
    ],
)
def test_unknown_data_cancels_pending_without_inventing_a_fill(kind: str) -> None:
    engine = MakerSimulationEngine(SimulationConfig(), None, T0)
    place(engine)
    later = T0 + timedelta(seconds=121 if kind == "gap" else 60)
    current_row, current_book, signal = row(later), book(later, "99.97", "99.98"), opportunity()
    rows = [current_row]
    books = {"BTC/EUR": current_book}
    if kind == "stale_quote":
        current_row["quote_ts"] = (later - timedelta(seconds=61)).isoformat()
    elif kind == "future_quote":
        current_row["quote_ts"] = (later + timedelta(seconds=1)).isoformat()
    elif kind == "stale_book":
        current_book["ts"] = (later - timedelta(seconds=61)).isoformat()
    elif kind == "future_book":
        current_book["ts"] = (later + timedelta(seconds=1)).isoformat()
    elif kind == "missing_row":
        rows = []
    elif kind == "missing_book":
        books = {}
    elif kind == "interrupted":
        signal["status"] = "interrupted"
        current_book = book(later)
        books = {"BTC/EUR": current_book}
    result = engine.step(rows, [signal], books, {"BTC/EUR": market()}, later)
    assert not result["positions"] and not result["pending_orders"]
    assert result["cash_eur"] == "50" and result["stats"]["equity_eur"] == "50"
    assert result["maker_orders"][0]["status"] == "cancelled"
    assert result["maker_orders"][0]["incomplete"] is True


@pytest.mark.parametrize(
    "kind", ["exit", "filter", "market_inactive", "precision_changed", "cost_minimum", "spread"]
)
def test_pending_revalidates_market_and_exit_filters_before_fill(kind: str) -> None:
    engine = MakerSimulationEngine(SimulationConfig(), None, T0)
    place(engine)
    later = T0 + timedelta(minutes=1)
    current_row, current_market, current_book = (
        row(later, score=50),
        market(),
        book(later),
    )
    if kind == "exit":
        current_row["score"] = 0
    elif kind == "filter":
        current_row["reasons"] = ["Volumen desconocido"]
    elif kind == "market_inactive":
        current_market["active"] = False
    elif kind == "precision_changed":
        current_market["precision"]["price"] = 0.1
    elif kind == "cost_minimum":
        current_market["limits"]["cost"]["min"] = 20
    else:
        current_book["bids"] = [["98", "100"]]
    result = engine.step(
        [current_row],
        [opportunity()],
        {"BTC/EUR": current_book},
        {"BTC/EUR": current_market},
        later,
    )
    assert not result["positions"] and not result["pending_orders"]
    assert result["cash_eur"] == "50" and result["maker_orders"][0]["status"] == "cancelled"


def test_reservations_occupy_slots_and_prevent_cash_overcommit() -> None:
    engine = MakerSimulationEngine(SimulationConfig(), None, T0)
    symbols = [f"COIN{index}/EUR" for index in range(6)]
    result = engine.step(
        [row(symbol=symbol) for symbol in symbols],
        [opportunity(str(index), symbol) for index, symbol in enumerate(symbols)],
        {symbol: book() for symbol in symbols},
        {symbol: market() for symbol in symbols},
        T0,
    )
    assert len(result["pending_orders"]) == 5 and len(engine.holding_symbols()) == 5
    assert result["cash_eur"] == "0" and result["stats"]["reserved_eur"] == "50"
    assert result["stats"]["equity_eur"] == "50"
    assert result["decisions"][-1]["action"] == "skip"


def test_restart_keeps_reservation_and_fills_only_once() -> None:
    engine = MakerSimulationEngine(SimulationConfig(), None, T0)
    saved = copy.deepcopy(place(engine))
    later = T0 + timedelta(minutes=1)
    restarted = MakerSimulationEngine(SimulationConfig(), saved, later)
    filled = copy.deepcopy(tick(restarted, later))
    repeated = tick(restarted, later)
    assert repeated["positions"] == filled["positions"]
    assert repeated["cash_eur"] == filled["cash_eur"]
    assert repeated["maker_orders"] == filled["maker_orders"]
    assert repeated["stats"]["maker_fills"] == 1
    assert saved["pending_orders"]["BTC/EUR"]["status"] == "pending"
    with pytest.raises(ValueError, match="configuración"):
        MakerSimulationEngine(SimulationConfig(), saved, later, pending_minutes=15)


def test_sell_remains_taker_and_pnl_includes_only_exit_fee() -> None:
    engine = MakerSimulationEngine(SimulationConfig(), None, T0)
    place(engine)
    tick(engine, T0 + timedelta(minutes=1), score=50)
    result = tick(engine, T0 + timedelta(minutes=2), bid="101", ask="101.02", score=0)
    trade = result["trades"][0]
    assert not result["positions"] and trade["entry_fee_eur"] == "0"
    assert D(trade["exit_fee_eur"]) == D(trade["quantity"]) * D("101") * D("0.0009")
    assert D(trade["pnl_eur"]) == D(trade["quantity"]) * D("101") * D("0.9991") - D(
        trade["investment_eur"]
    )
    assert D(result["cash_eur"]) == 50 + D(trade["pnl_eur"])


@pytest.mark.parametrize("kind", ["exit", "filters", "spread_and_costs", "interrupted"])
def test_observed_cross_fills_before_new_cancellation_conditions(kind: str) -> None:
    engine = MakerSimulationEngine(SimulationConfig(), None, T0)
    place(engine)
    later = T0 + timedelta(minutes=1)
    current_row = row(later, score=50)
    current_book = book(later, "99.97", "99.98")
    signal = opportunity()
    if kind == "exit":
        current_row["score"] = 0
    elif kind == "filters":
        current_row["reasons"] = ["Volumen desconocido"]
    elif kind == "spread_and_costs":
        current_book["bids"] = [["90", "100"]]
    else:
        signal["status"] = "interrupted"
    result = engine.step(
        [current_row], [signal], {"BTC/EUR": current_book}, {"BTC/EUR": market()}, later
    )
    assert result["maker_orders"][0]["status"] == "filled"
    assert result["stats"]["maker_cancelled"] == 0
    assert len(result["trades"]) == 1 and not result["positions"]
    trade = result["trades"][0]
    assert trade["entry"] == "99.99" and D(trade["exit_fee_eur"]) > 0
    assert D(trade["pnl_eur"]) < 0 and D(result["cash_eur"]) < 50
    actions = [decision["action"] for decision in result["decisions"]]
    assert actions == (
        ["place", "buy", "wait", "sell"] if kind == "interrupted" else ["place", "buy", "sell"]
    )


def test_observed_buy_cross_with_no_sale_depth_holds_actual_fictitious_position() -> None:
    engine = MakerSimulationEngine(SimulationConfig(), None, T0)
    place(engine)
    later = T0 + timedelta(minutes=1)
    current_book = book(later, "99.97", "99.98")
    current_book["bids"] = [["99.97", "0.00001"]]
    result = engine.step(
        [row(later, score=0)],
        [opportunity()],
        {"BTC/EUR": current_book},
        {"BTC/EUR": market()},
        later,
    )
    assert result["maker_orders"][0]["status"] == "filled"
    assert result["positions"]["BTC/EUR"]["status"] == "waiting_data"
    assert result["stats"]["equity_eur"] is None
    assert not result["trades"] and D(result["cash_eur"]) < 41


def test_expiry_after_unobserved_gap_stays_incomplete() -> None:
    engine = MakerSimulationEngine(SimulationConfig(), None, T0)
    place(engine)
    result = tick(engine, T0 + timedelta(hours=2))
    assert result["maker_orders"][0]["status"] == "expired"
    assert result["maker_orders"][0]["incomplete"] is True
    assert result["cash_eur"] == "50" and not result["positions"]


def test_new_pending_order_preserves_equity_when_another_position_exists() -> None:
    engine = MakerSimulationEngine(SimulationConfig(), None, T0)
    place(engine)
    first = copy.deepcopy(tick(engine, T0 + timedelta(minutes=1), score=50))
    later = T0 + timedelta(minutes=2)
    result = engine.step(
        [row(later, score=50), row(later, "ETH/EUR")],
        [opportunity(), opportunity("two", "ETH/EUR", created_at=later.isoformat())],
        {"BTC/EUR": book(later, "99.97", "99.98"), "ETH/EUR": book(later)},
        {"BTC/EUR": market(), "ETH/EUR": market()},
        later,
    )
    assert result["stats"]["equity_eur"] == first["stats"]["equity_eur"]
    assert D(result["cash_eur"]) == D(first["cash_eur"]) - 10
    assert result["stats"]["reserved_eur"] == "10"


@pytest.mark.parametrize(
    "kind",
    [
        "historical",
        "future",
        "locked",
        "no_tick",
        "stale",
        "insufficient_exit_depth",
        "nonpositive_net",
    ],
)
def test_invalid_placements_never_reserve_or_fill(kind: str) -> None:
    engine = MakerSimulationEngine(SimulationConfig(), None, T0)
    current_row, current_book, current_market, signal = row(), book(), market(), opportunity()
    if kind == "historical":
        signal["created_at"] = (T0 - timedelta(seconds=1)).isoformat()
    elif kind == "future":
        signal["created_at"] = (T0 + timedelta(seconds=1)).isoformat()
    elif kind == "locked":
        current_book = book(bid="100", ask="100")
    elif kind == "no_tick":
        del current_market["precision"]["price"]
    elif kind == "stale":
        current_book["ts"] = (T0 - timedelta(seconds=61)).isoformat()
    elif kind == "insufficient_exit_depth":
        current_book["bids"] = [["99.99", "0.00001"]]
    else:
        current_row["atr"] = 0.00001
    result = engine.step(
        [current_row], [signal], {"BTC/EUR": current_book}, {"BTC/EUR": current_market}, T0
    )
    assert not result["pending_orders"] and not result["positions"]
    assert result["cash_eur"] == "50" and result["stats"]["equity_eur"] == "50"
