from __future__ import annotations

import ast
import copy
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from invertio.simulation import engine as engine_module
from invertio.simulation.config import SimulationConfig
from invertio.simulation.engine import SimulationEngine

T0 = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
D = Decimal


def opportunity(
    ident: str = "one", symbol: str = "BTC/EUR", at: datetime = T0, **overrides: Any
) -> dict[str, Any]:
    return {
        "id": ident,
        "market": f"revolutx:{symbol}",
        "created_at": at.isoformat(),
        "rule_version": "source-score-v1",
        "status": "open",
        "entry": 123456,
        "stop": 123455,
        "target": 123460,
        **overrides,
    }


def row(
    symbol: str = "BTC/EUR", at: datetime = T0, ident: str = "one", **overrides: Any
) -> dict[str, Any]:
    return {
        "market": f"revolutx:{symbol}",
        "opportunity_id": ident,
        "state": "open",
        "score": 70,
        "price": 100,
        "atr_pct": 1,
        "quote_ts": at.isoformat(),
        "bar_ts": at.isoformat(),
        "reasons": [],
        **overrides,
    }


def book(
    at: datetime = T0, bid: str = "99.99", ask: str = "100.01", quantity: str = "100"
) -> dict[str, Any]:
    return {"ts": at.isoformat(), "bids": [[bid, quantity]], "asks": [[ask, quantity]]}


def market(**overrides: Any) -> dict[str, Any]:
    return {
        "active": True,
        "spot": True,
        "quote": "EUR",
        "precision": {"amount": 0.00001},
        "limits": {"amount": {"min": 0.00001, "max": 1000}, "cost": {"min": 1}},
        **overrides,
    }


def buy(engine: SimulationEngine) -> dict[str, Any]:
    return engine.step([row()], [opportunity()], {"BTC/EUR": book()}, {"BTC/EUR": market()}, T0)


def test_buy_uses_current_book_and_precision_with_ten_euros_including_fee() -> None:
    engine = SimulationEngine(SimulationConfig(), None, T0)
    state = buy(engine)
    trade = state["positions"]["BTC/EUR"]
    quantity, entry, fee = D(trade["quantity"]), D(trade["entry"]), D(trade["entry_fee_eur"])
    assert quantity % D("0.00001") == 0
    assert entry == D("100.01")  # Ignores the opportunity's historical/original entry.
    assert fee == quantity * entry * D("0.0009")
    assert D(trade["investment_eur"]) <= D("10")
    assert D(state["cash_eur"]) + D(trade["investment_eur"]) == 50
    assert D(state["stats"]["equity_eur"]) == D(state["cash_eur"]) + quantity * D("99.99") * D(
        "0.9991"
    )
    assert D(state["stats"]["realized_pnl_eur"]) == 0
    assert trade["opened_at"] == T0.isoformat()
    assert trade["source_created_at"] == T0.isoformat()
    assert trade["source_rule_version"] == "source-score-v1"
    assert (D(trade["stop"]), D(trade["target"])) == (D("98.01"), D("103.01"))
    assert state["hypothetical"] is True
    json.dumps(state, allow_nan=False)


@pytest.mark.parametrize(("max_positions", "skip_reason"), [(5, "plazas"), (10, "Efectivo")])
def test_slots_and_cash_skips_are_permanent_even_when_capital_is_released(
    max_positions: int, skip_reason: str
) -> None:
    engine = SimulationEngine(SimulationConfig(max_positions=max_positions), None, T0)
    symbols = [f"COIN{index}/EUR" for index in range(6)]
    opportunities = [opportunity(str(index), symbol) for index, symbol in enumerate(symbols)]
    rows = [row(symbol, ident=str(index)) for index, symbol in enumerate(symbols)]
    # The sixth book is deliberately absent: cash/slot gating must happen first.
    books = {symbol: book() for symbol in symbols[:5]}
    state = engine.step(rows, opportunities, books, {symbol: market() for symbol in symbols}, T0)
    assert len(state["positions"]) == 5
    assert D(state["cash_eur"]) >= 0
    assert skip_reason in state["decisions"][-1]["reason"]
    later = T0 + timedelta(minutes=1)
    rows = [
        row(symbol, later, str(index), score=30 if index == 0 else 70)
        for index, symbol in enumerate(symbols)
    ]
    engine.step(
        rows,
        opportunities,
        {symbol: book(later) for symbol in symbols},
        {symbol: market() for symbol in symbols},
        later,
    )
    assert symbols[5] not in engine.holding_symbols()
    assert len(engine.state["positions"]) == 4
    assert len(engine.state["trades"]) == 1
    assert [
        decision["action"]
        for decision in engine.state["decisions"]
        if decision["opportunity_id"] == "5"
    ] == ["skip"]


def test_restart_duplicates_and_same_time_equity_are_idempotent() -> None:
    engine = SimulationEngine(SimulationConfig(), None, T0)
    initial = copy.deepcopy(buy(engine))
    initial["analysis_cycle"] = 17
    restarted = SimulationEngine(SimulationConfig(), initial, T0)
    result = buy(restarted)
    assert result["cash_eur"] == initial["cash_eur"]
    assert len(result["decisions"]) == 1
    assert len(result["equity"]) == 1
    assert result["analysis_cycle"] == 17
    assert initial["positions"]["BTC/EUR"]["status"] == "open"
    assert restarted.holding_symbols() == ["BTC/EUR"]


@pytest.mark.parametrize(
    "kind",
    [
        "historical_signal",
        "future_signal",
        "stale_book",
        "future_book",
        "stale_quote",
        "future_bar",
    ],
)
def test_entry_never_uses_historical_or_future_information(kind: str) -> None:
    engine = SimulationEngine(SimulationConfig(), None, T0)
    signal, current_row, current_book = opportunity(), row(), book()
    if kind == "historical_signal":
        signal["created_at"] = (T0 - timedelta(seconds=1)).isoformat()
    elif kind == "future_signal":
        signal["created_at"] = (T0 + timedelta(seconds=1)).isoformat()
    elif kind == "stale_book":
        current_book["ts"] = (T0 - timedelta(seconds=61)).isoformat()
    elif kind == "future_book":
        current_book["ts"] = (T0 + timedelta(seconds=1)).isoformat()
    elif kind == "stale_quote":
        current_row["quote_ts"] = (T0 - timedelta(seconds=61)).isoformat()
    else:
        current_row["bar_ts"] = (T0 + timedelta(minutes=1)).isoformat()
    result = engine.step(
        [current_row], [signal], {"BTC/EUR": current_book}, {"BTC/EUR": market()}, T0
    )
    assert not result["positions"] and result["cash_eur"] == "50"
    if kind == "historical_signal":
        assert result["decisions"] == []
    else:
        assert result["decisions"][0]["action"] == "skip"


def test_missing_book_holds_capital_and_recovery_sells_at_current_bid() -> None:
    engine = SimulationEngine(SimulationConfig(), None, T0)
    opened = copy.deepcopy(buy(engine))
    later = T0 + timedelta(minutes=1)
    state = engine.step([row(at=later)], [opportunity()], {}, {"BTC/EUR": market()}, later)
    assert state["positions"]["BTC/EUR"]["status"] == "waiting_data"
    assert state["cash_eur"] == opened["cash_eur"]
    assert state["stats"]["equity_eur"] is None
    assert state["stats"]["realized_pnl_eur"] == "0"
    assert not state["trades"]
    recovered_at = later + timedelta(minutes=1)
    state = engine.step(
        [row(at=recovered_at)],
        [opportunity()],
        {"BTC/EUR": book(recovered_at, "94", "94.02")},
        {"BTC/EUR": market()},
        recovered_at,
    )
    sold = state["trades"][0]
    assert D(sold["exit_price"]) == 94
    assert sold["closed_at"] == recovered_at.isoformat()
    assert sold["reason"] == "Recuperación tras falta de datos"
    assert sold["had_data_gap"] is True
    pnl = D(sold["quantity"]) * D("94") * D("0.9991") - D(sold["investment_eur"])
    assert D(sold["pnl_eur"]) == pnl
    assert D(state["cash_eur"]) == D("50") + pnl


def test_restart_gap_exits_at_first_current_book_not_historical_signal_price() -> None:
    engine = SimulationEngine(SimulationConfig(), None, T0)
    saved = copy.deepcopy(buy(engine))
    later = T0 + timedelta(hours=12)
    restarted = SimulationEngine(SimulationConfig(), saved, later)
    state = restarted.step(
        [row(at=later)],
        [opportunity()],
        {"BTC/EUR": book(later, "90", "90.02")},
        {"BTC/EUR": market()},
        later,
    )
    assert not state["positions"]
    assert state["trades"][0]["had_data_gap"] is True
    assert state["trades"][0]["reason"] == "Recuperación tras falta de datos"
    assert state["trades"][0]["exit_price"] == "90"
    assert state["trades"][0]["closed_at"] == later.isoformat()


def test_insufficient_exit_depth_does_not_invent_a_partial_liquidation() -> None:
    engine = SimulationEngine(SimulationConfig(), None, T0)
    opened = copy.deepcopy(buy(engine))
    later = T0 + timedelta(minutes=1)
    shallow = book(later, "90", "90.02", "0.00001")
    state = engine.step(
        [row(at=later, score=20)],
        [opportunity()],
        {"BTC/EUR": shallow},
        {"BTC/EUR": market()},
        later,
    )
    assert not state["trades"]
    assert state["cash_eur"] == opened["cash_eur"]
    assert state["positions"]["BTC/EUR"]["quantity"] == opened["positions"]["BTC/EUR"]["quantity"]
    assert state["positions"]["BTC/EUR"]["status"] == "waiting_data"


@pytest.mark.parametrize(
    ("bid", "ask", "reason"), [("97", "97.02", "Stop"), ("104", "104.02", "Objetivo")]
)
def test_stop_and_target_exit_at_executable_current_price_not_the_level(
    bid: str, ask: str, reason: str
) -> None:
    engine = SimulationEngine(SimulationConfig(), None, T0)
    buy(engine)
    later = T0 + timedelta(minutes=1)
    state = engine.step(
        [row(at=later)],
        [opportunity()],
        {"BTC/EUR": book(later, bid, ask)},
        {"BTC/EUR": market()},
        later,
    )
    trade = state["trades"][0]
    assert D(trade["exit_price"]) == D(bid)
    assert trade["exit_price"] not in {trade["stop"], trade["target"]}
    assert reason in trade["reason"]


def test_exits_run_before_new_entries_and_released_cash_can_fund_only_new_signals() -> None:
    engine = SimulationEngine(SimulationConfig(initial_eur="10", max_positions=1), None, T0)
    buy(engine)
    later = T0 + timedelta(minutes=1)
    new = opportunity("two", "ETH/EUR", later)
    state = engine.step(
        [row(at=later, score=20), row("ETH/EUR", later, "two")],
        [opportunity(), new],
        {"BTC/EUR": book(later, "101", "101.02"), "ETH/EUR": book(later)},
        {"BTC/EUR": market(), "ETH/EUR": market()},
        later,
    )
    assert engine.holding_symbols() == ["ETH/EUR"]
    assert [decision["action"] for decision in state["decisions"]] == ["buy", "sell", "buy"]
    assert D(state["cash_eur"]) >= 0


def test_interrupted_signal_waits_then_liquidates_after_data_recovery() -> None:
    engine = SimulationEngine(SimulationConfig(), None, T0)
    buy(engine)
    later = T0 + timedelta(minutes=1)
    interrupted = opportunity(status="interrupted")
    state = engine.step([row(at=later)], [interrupted], {}, {"BTC/EUR": market()}, later)
    assert not state["trades"] and state["stats"]["equity_eur"] is None
    next_cycle = later + timedelta(minutes=1)
    state = engine.step(
        [row(at=next_cycle)],
        [interrupted],
        {"BTC/EUR": book(next_cycle)},
        {"BTC/EUR": market()},
        next_cycle,
    )
    assert state["trades"][0]["reason"] == "Recuperación tras falta de datos"


def test_interrupted_signal_with_fresh_book_liquidates_in_same_cycle() -> None:
    engine = SimulationEngine(SimulationConfig(), None, T0)
    buy(engine)
    later = T0 + timedelta(minutes=1)
    state = engine.step(
        [row(at=later)],
        [opportunity(status="interrupted")],
        {"BTC/EUR": book(later, bid="95", ask="95.02")},
        {"BTC/EUR": market()},
        later,
    )
    assert not state["positions"]
    sold = state["trades"][0]
    assert sold["reason"] == "Recuperación tras falta de datos"
    assert sold["had_data_gap"] is True
    assert sold["closed_at"] == later.isoformat()
    assert D(sold["exit_price"]) == D("95")


def test_closed_ambiguous_signal_is_liquidated_now_without_claiming_its_historical_result() -> None:
    engine = SimulationEngine(SimulationConfig(), None, T0)
    buy(engine)
    later = T0 + timedelta(minutes=1)
    state = engine.step(
        [row(at=later)],
        [opportunity(status="closed", outcome="ambiguous")],
        {"BTC/EUR": book(later)},
        {"BTC/EUR": market()},
        later,
    )
    assert "ambiguous" in state["trades"][0]["reason"]
    assert state["trades"][0]["exit_price"] == "99.99"
    assert state["trades"][0]["pnl_eur"] is not None


def test_entry_notes_and_zero_volume_alone_do_not_force_a_sale() -> None:
    engine = SimulationEngine(SimulationConfig(), None, T0)
    buy(engine)
    later = T0 + timedelta(minutes=1)
    state = engine.step(
        [
            row(
                at=later,
                score=60,
                reasons=["Nota inferior a 70", "La última vela no acredita volumen negociado"],
            )
        ],
        [opportunity()],
        {"BTC/EUR": book(later)},
        {"BTC/EUR": market()},
        later,
    )
    assert state["positions"]["BTC/EUR"]["status"] == "open"
    assert not state["trades"]


@pytest.mark.parametrize(
    "kind",
    ["amount_minimum", "cost_minimum", "amount_step", "depth", "slippage", "spread", "target_net"],
)
def test_entry_rejects_non_executable_or_non_positive_net_opportunities(kind: str) -> None:
    engine = SimulationEngine(SimulationConfig(), None, T0)
    current_market, current_book, current_row = market(), book(), row()
    if kind == "amount_minimum":
        current_market["limits"]["amount"]["min"] = 1
    elif kind == "cost_minimum":
        current_market["limits"]["cost"]["min"] = 20
    elif kind == "amount_step":
        current_market["precision"]["amount"] = 1
    elif kind == "depth":
        current_book["bids"] = [["99.99", "0.0001"]]
    elif kind == "slippage":
        current_book["asks"] = [["100.01", "0.01"], ["101", "100"]]
    elif kind == "spread":
        current_book["bids"] = [["99", "100"]]
    else:
        current_row["atr_pct"] = 0.001
    result = engine.step(
        [current_row], [opportunity()], {"BTC/EUR": current_book}, {"BTC/EUR": current_market}, T0
    )
    assert not result["positions"] and result["cash_eur"] == "50"
    assert result["decisions"][0]["action"] == "skip"


def test_configuration_mismatch_fails_without_resetting_saved_cash() -> None:
    saved = copy.deepcopy(buy(SimulationEngine(SimulationConfig(), None, T0)))
    original = copy.deepcopy(saved)
    with pytest.raises(ValueError, match="configuración"):
        SimulationEngine(SimulationConfig(fee_pct="0.1"), saved, T0)
    assert saved == original
    with pytest.raises(ValidationError):
        SimulationConfig(initial_eur="9", allocation_eur="10")
    with pytest.raises(ValidationError):
        SimulationConfig(fee_pct="NaN")


def test_pure_engine_has_no_real_execution_or_network_dependencies() -> None:
    source = Path(engine_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    imported += [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    assert not any(
        any(word in name for word in ("manual", "execution", "httpx", "ccxt", "socket"))
        for name in imported
    )
