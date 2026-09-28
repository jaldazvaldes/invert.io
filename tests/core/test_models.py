from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from invertio.core.models import (
    Bar,
    Fill,
    InvalidOrderTransition,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    Signal,
    SignalAction,
    round_down,
    to_decimal,
)

D = Decimal


def _buy_request(qty: str = "0.002", **kwargs: object) -> OrderRequest:
    return OrderRequest(
        venue="revolutx",
        symbol="BTC/EUR",
        side=Side.BUY,
        type=OrderType.MARKET,
        quantity=D(qty),
        strategy_id="test",
        stop_loss=D("90"),
        **kwargs,  # type: ignore[arg-type]
    )


def _fill(side: Side, qty: str, price: str, ts: datetime, fee: str = "0") -> Fill:
    return Fill(
        client_order_id="x",
        venue="revolutx",
        symbol="BTC/EUR",
        side=side,
        quantity=D(qty),
        price=D(price),
        fee=D(fee),
        fee_currency="EUR",
        ts=ts,
    )


class TestBar:
    def test_close_time(self, t0: datetime) -> None:
        bar = Bar("revolutx", "BTC/EUR", "5m", t0, 100, 110, 95, 105, 1.5)
        assert bar.close_time == t0 + timedelta(minutes=5)

    def test_rejects_naive_or_non_utc_datetimes(self, t0: datetime) -> None:
        with pytest.raises(ValueError, match="UTC"):
            Bar("revolutx", "BTC/EUR", "5m", t0.replace(tzinfo=None), 1, 1, 1, 1, 0)
        madrid = timezone(timedelta(hours=1))
        with pytest.raises(ValueError, match="UTC"):
            Bar("revolutx", "BTC/EUR", "5m", t0.astimezone(madrid), 1, 1, 1, 1, 0)

    def test_rejects_incoherent_bar(self, t0: datetime) -> None:
        with pytest.raises(ValueError):
            Bar("revolutx", "BTC/EUR", "5m", t0, 100, 90, 95, 100, 1)


class TestSignal:
    def test_buy_requires_stop_loss_below_price(self, t0: datetime) -> None:
        with pytest.raises(ValueError, match="stop_loss"):
            Signal("s", "revolutx", "BTC/EUR", SignalAction.BUY, t0, price=100)
        with pytest.raises(ValueError, match="por debajo"):
            Signal("s", "revolutx", "BTC/EUR", SignalAction.BUY, t0, price=100, stop_loss=101)
        with pytest.raises(ValueError, match="take_profit"):
            Signal(
                "s",
                "revolutx",
                "BTC/EUR",
                SignalAction.BUY,
                t0,
                price=100,
                stop_loss=98,
                take_profit=99,
            )

    def test_close_needs_no_stop(self, t0: datetime) -> None:
        signal = Signal("s", "revolutx", "BTC/EUR", SignalAction.CLOSE, t0, price=100)
        assert signal.stop_loss is None
        assert len(signal.id) == 32


class TestOrderRequest:
    def test_validation(self) -> None:
        with pytest.raises(ValueError, match="positiva"):
            _buy_request("0")
        with pytest.raises(ValueError, match="limit_price"):
            OrderRequest("v", "S", Side.BUY, OrderType.LIMIT, D(1), "s")
        with pytest.raises(ValueError, match="mercado"):
            _buy_request(post_only=True)

    def test_brackets(self) -> None:
        with pytest.raises(ValueError, match="stop_loss"):
            OrderRequest("v", "S", Side.BUY, OrderType.MARKET, D(1), "s")
        with pytest.raises(ValueError, match="take_profit"):
            _buy_request(take_profit=D("80"))
        with pytest.raises(ValueError, match="Solo las compras"):
            OrderRequest("v", "S", Side.SELL, OrderType.MARKET, D(1), "s", stop_loss=D(1))

    def test_decimal_helpers(self) -> None:
        assert to_decimal(0.1) == D("0.1")
        assert round_down(D("0.123456789"), D("0.00000001")) == D("0.12345678")
        assert round_down(D("91234.567"), D("0.01")) == D("91234.56")

    def test_client_order_ids_are_unique(self) -> None:
        assert _buy_request().client_order_id != _buy_request().client_order_id


class TestOrderStateMachine:
    def test_partial_then_full_fill(self, t0: datetime) -> None:
        order = Order(_buy_request("1.0"), created_at=t0)
        order.transition(OrderStatus.SUBMITTED, t0)
        order.apply_fill(D("0.4"), D("100"), t0)
        status_after_partial = order.status
        order.apply_fill(D("0.6"), D("110"), t0)
        assert status_after_partial is OrderStatus.PARTIALLY_FILLED
        assert order.status is OrderStatus.FILLED
        assert order.avg_fill_price == D("106")
        assert order.remaining_quantity == 0
        assert order.status.is_terminal

    def test_cannot_fill_before_submission(self, t0: datetime) -> None:
        order = Order(_buy_request(), created_at=t0)
        with pytest.raises(InvalidOrderTransition):
            order.apply_fill(D("0.002"), D("100"), t0)

    def test_terminal_states_are_final(self, t0: datetime) -> None:
        order = Order(_buy_request(), created_at=t0)
        order.transition(OrderStatus.REJECTED, t0, reason="saldo insuficiente")
        assert order.reject_reason == "saldo insuficiente"
        with pytest.raises(InvalidOrderTransition):
            order.transition(OrderStatus.SUBMITTED, t0)

    def test_overfill_rejected(self, t0: datetime) -> None:
        order = Order(_buy_request("1"), created_at=t0)
        order.transition(OrderStatus.SUBMITTED, t0)
        with pytest.raises(ValueError, match="supera"):
            order.apply_fill(D("1.5"), D("100"), t0)


class TestPosition:
    def test_round_trip_pnl_includes_fees(self, t0: datetime) -> None:
        position = Position("revolutx", "BTC/EUR")
        position.apply_fill(_fill(Side.BUY, "0.5", "100", t0), fee_in_quote=D("0.05"))
        position.apply_fill(_fill(Side.BUY, "0.5", "120", t0), fee_in_quote=D("0.05"))
        assert position.avg_price == D("110")
        assert position.unrealized_pnl(D("130")) == D("20")

        position.apply_fill(_fill(Side.SELL, "1", "130", t0), fee_in_quote=D("0.10"))
        assert not position.is_open
        assert position.realized_pnl == D("20") - D("0.20")
        assert position.fees_paid == D("0.20")

    def test_no_shorts(self, t0: datetime) -> None:
        position = Position("revolutx", "BTC/EUR")
        with pytest.raises(ValueError, match="cortos"):
            position.apply_fill(_fill(Side.SELL, "1", "100", t0), fee_in_quote=D(0))
