from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from invertio.config import AppConfig
from invertio.core.bus import EventBus
from invertio.core.clock import SimClock
from invertio.core.events import OrderFilled, OrderUpdated
from invertio.core.models import Fill, OrderRequest, OrderStatus, OrderType, Side
from invertio.execution.sim import SimBroker
from invertio.portfolio import Portfolio
from tests.helpers import bar, minutes, repo_config

D = Decimal


class Harness:
    def __init__(self, t0: datetime, config: AppConfig | None = None, cash: str = "1000") -> None:
        config = config or repo_config()
        self.bus = EventBus(strict=True)
        self.clock = SimClock(t0)
        self.portfolio = Portfolio(self.bus, {"revolutx": D(cash)}, {"revolutx": "EUR"})
        self.broker = SimBroker(self.bus, self.clock, config.venue("revolutx"), self.portfolio)
        self.fills: list[Fill] = []
        self.statuses: list[OrderStatus] = []
        self.bus.subscribe(OrderFilled, lambda e: self.fills.append(e.fill))
        self.bus.subscribe(OrderUpdated, lambda e: self.statuses.append(e.order.status))

    async def buy(self, **kw: object) -> None:
        params: dict[str, object] = {
            "venue": "revolutx",
            "symbol": "BTC/EUR",
            "side": Side.BUY,
            "type": OrderType.MARKET,
            "quantity": D("0.01"),
            "strategy_id": "t",
            "stop_loss": D("95"),
        }
        params.update(kw)
        await self.broker.submit(OrderRequest(**params))  # type: ignore[arg-type]


async def test_market_buy_fills_at_next_open_with_costs(t0: datetime) -> None:
    h = Harness(t0)
    await h.buy()
    assert h.statuses == [OrderStatus.SUBMITTED]
    await h.broker.process_bar(bar(t0, 100, 101, 99, 100.5))
    fill = h.fills[0]
    # medio spread (0,01 %) + deslizamiento (0,02 %) = 0,03 % sobre la apertura
    assert fill.price == D("100.03")
    assert fill.fee == D("0.01") * D("100.03") * D("0.0009")  # taker 0,09 %
    assert fill.ts == t0
    assert h.portfolio.cash("revolutx") == D(1000) - fill.notional - fill.fee
    assert h.statuses[-1] is OrderStatus.FILLED


async def test_live_order_seconds_after_close_fills_with_that_bar(t0: datetime) -> None:
    """En vivo la orden nace unos segundos tras el cierre: se ejecuta en la vela que abre ahí."""
    h = Harness(t0)
    h.clock.set(t0 + timedelta(seconds=5))
    await h.buy()
    await h.broker.process_bar(bar(t0, 100, 101, 99, 100.5))
    assert len(h.fills) == 1
    assert h.fills[0].ts == t0 + timedelta(seconds=5)  # nunca antes de crear la orden
    assert h.fills[0].price == D("100.03")  # pero al precio de apertura de la vela


async def test_order_sent_after_bar_open_waits_for_next_bar(t0: datetime) -> None:
    h = Harness(t0)
    h.clock.set(t0 + minutes(1))
    await h.buy()
    await h.broker.process_bar(bar(t0, 100, 101, 99, 100.5))
    assert h.fills == []
    await h.broker.process_bar(bar(t0 + minutes(5), 100.5, 101, 100, 100.7))
    assert len(h.fills) == 1


async def test_post_only_limit_fills_only_if_price_trades_below(t0: datetime) -> None:
    h = Harness(t0)
    await h.buy(type=OrderType.LIMIT, limit_price=D("99.5"), post_only=True)
    await h.broker.process_bar(bar(t0, 100, 101, 99.5, 100.5))  # toca 99,5 pero no la cruza
    assert h.fills == []
    await h.broker.process_bar(bar(t0 + minutes(5), 100, 100.2, 99.4, 99.8))
    fill = h.fills[0]
    assert (fill.price, fill.fee) == (D("99.5"), D(0))  # maker 0 %


async def test_limit_order_expires_after_ttl(t0: datetime) -> None:
    h = Harness(t0)
    await h.buy(type=OrderType.LIMIT, limit_price=D("90"), post_only=True)
    await h.broker.process_bar(bar(t0, 100, 101, 99, 100))
    await h.broker.process_bar(bar(t0 + minutes(5), 100, 101, 99, 100))
    assert h.fills == []
    assert h.statuses[-1] is OrderStatus.CANCELED
    assert h.broker.open_orders() == []


@pytest.mark.parametrize(
    ("ohlc", "expected_price", "reason"),
    [
        ((94, 94.5, 93, 94), D("94") * D("0.9998"), "stop_loss"),  # abre por debajo: hueco
        ((99, 99.5, 94, 96), D("95") * D("0.9998"), "stop_loss"),  # toca el stop
        ((99, 106, 94, 105), D("95") * D("0.9998"), "stop_loss"),  # stop y TP: se asume el stop
        ((101, 106, 100, 105), D("105"), "take_profit"),
    ],
)
async def test_brackets(
    t0: datetime, ohlc: tuple[float, float, float, float], expected_price: Decimal, reason: str
) -> None:
    h = Harness(t0)
    await h.buy(take_profit=D("105"))
    await h.broker.process_bar(bar(t0, 100, 100.5, 99.5, 100))
    await h.broker.process_bar(bar(t0 + minutes(5), *ohlc))
    exit_fill = h.fills[-1]
    assert exit_fill.side is Side.SELL
    assert exit_fill.price == expected_price
    assert not h.portfolio.position("revolutx", "BTC/EUR").is_open
    assert h.portfolio.closed_trades[-1].exit_reason == reason
    # sin posición, los stops ya no actúan
    await h.broker.process_bar(bar(t0 + minutes(10), 80, 81, 79, 80))
    assert len(h.fills) == 2


async def test_insufficient_cash_rejects(t0: datetime) -> None:
    h = Harness(t0, cash="0.5")
    await h.buy()
    await h.broker.process_bar(bar(t0, 100, 101, 99, 100))
    assert h.fills == []
    assert h.statuses[-1] is OrderStatus.REJECTED


async def test_rejects_orders_for_other_venues(t0: datetime) -> None:
    h = Harness(t0)
    with pytest.raises(ValueError, match="simulador"):
        await h.buy(venue="trading212", symbol="AAPL")
