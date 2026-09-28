from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from invertio.config import AppConfig
from invertio.core.bus import EventBus
from invertio.core.clock import SimClock
from invertio.core.events import BarClosed, EngineState, OrderFilled, RiskDecision
from invertio.core.models import Fill, OrderType, Side, Signal, SignalAction
from invertio.portfolio import Portfolio
from invertio.risk import RiskManager
from tests.helpers import make_bars, repo_config, with_venue

D = Decimal


def _setup(
    t0: datetime, config: AppConfig | None = None, cash: str = "100", **kwargs: Any
) -> tuple[EventBus, SimClock, Portfolio, RiskManager]:
    config = config or repo_config()
    bus = EventBus(strict=True)
    clock = SimClock(t0)
    portfolio = Portfolio(
        bus,
        {"revolutx": D(cash), "trading212": D(cash)},
        {"revolutx": "EUR", "trading212": "USD"},
    )
    risk = RiskManager(bus, clock, config, portfolio, **kwargs)
    return bus, clock, portfolio, risk


def _buy(t0: datetime, price: float = 50_000, stop: float = 49_000, **kw: Any) -> Signal:
    defaults: dict[str, Any] = {"venue": "revolutx", "symbol": "BTC/EUR"}
    defaults.update(kw)
    return Signal(
        "test", defaults["venue"], defaults["symbol"], SignalAction.BUY, t0, price, stop_loss=stop
    )


def _close(t0: datetime, symbol: str = "BTC/EUR") -> Signal:
    return Signal("test", "revolutx", symbol, SignalAction.CLOSE, t0, price=50_000)


async def _fill(bus: EventBus, t0: datetime, side: Side, qty: str, price: str) -> None:
    await bus.publish(
        OrderFilled(Fill("x", "revolutx", "BTC/EUR", side, D(qty), D(price), D(0), "EUR", t0))
    )


class TestSizing:
    def test_risk_based_size_with_costs(self, t0: datetime) -> None:
        _, _, _, risk = _setup(t0)
        decision = risk.evaluate(_buy(t0, price=50_000, stop=49_000))
        assert decision.approved, decision.reason
        order = decision.order
        assert order is not None
        # Riesgo 0,5 % de 100 € = 0,50 €. Riesgo por unidad = 1000 € de distancia al stop
        # + costes de ida y vuelta (2·(0,09+0+0,02) + 0,02 = 0,24 % de 50 000 = 120 €).
        assert order.quantity == D("0.00044642")
        assert order.quantity * D(50_000) < D("50")  # dentro del tope del 50 % del capital
        assert order.stop_loss == D("49000.00")

    def test_position_cap_limits_tight_stops(self, t0: datetime) -> None:
        _, _, _, risk = _setup(t0)
        order = risk.evaluate(_buy(t0, price=50_000, stop=49_990)).order
        assert order is not None
        assert order.quantity * D(50_000) <= D("50")

    def test_post_only_limit_on_revolutx_market_on_trading212(self, t0: datetime) -> None:
        _, _, _, risk = _setup(t0)
        crypto = risk.evaluate(_buy(t0)).order
        stock = risk.evaluate(_buy(t0, 200, 195, venue="trading212", symbol="AAPL")).order
        assert crypto is not None and stock is not None
        assert (crypto.type, crypto.post_only, crypto.limit_price) == (
            OrderType.LIMIT,
            True,
            D("50000.00"),
        )
        assert (stock.type, stock.post_only, stock.limit_price) == (OrderType.MARKET, False, None)

    def test_below_min_notional_rejected(self, t0: datetime) -> None:
        _, _, _, risk = _setup(t0, cash="0.1")  # mínimo de Revolut X: 0,10 €
        decision = risk.evaluate(_buy(t0))
        assert not decision.approved
        assert "mínimo" in decision.reason


class TestRules:
    def test_whitelist_and_unknown_venue(self, t0: datetime) -> None:
        _, _, _, risk = _setup(t0)
        assert "lista blanca" in risk.evaluate(_buy(t0, symbol="DOGE/EUR")).reason
        assert "desconocido" in risk.evaluate(_buy(t0, venue="kraken")).reason

    async def test_no_second_entry_and_close_rules(self, t0: datetime) -> None:
        bus, _, _, risk = _setup(t0)
        assert "no hay posición" in risk.evaluate(_close(t0)).reason
        await _fill(bus, t0, Side.BUY, "0.0004", "50000")
        assert "ya hay posición" in risk.evaluate(_buy(t0)).reason
        close = risk.evaluate(_close(t0))
        assert close.approved and close.order is not None
        assert (close.order.side, close.order.quantity) == (Side.SELL, D("0.0004"))

    async def test_max_open_positions(self, t0: datetime) -> None:
        bus, _, _, risk = _setup(t0, repo_config(max_open_positions=1))
        await _fill(bus, t0, Side.BUY, "0.0004", "50000")
        decision = risk.evaluate(_buy(t0, 2500, 2400, symbol="ETH/EUR"))
        assert "máximo de posiciones" in decision.reason

    def test_orders_per_hour_window(self, t0: datetime) -> None:
        _, clock, _, risk = _setup(t0, repo_config(max_orders_per_hour=1, max_open_positions=5))
        assert risk.evaluate(_buy(t0)).approved
        assert "por hora" in risk.evaluate(_buy(t0, 2500, 2400, symbol="ETH/EUR")).reason
        clock.set(t0 + timedelta(hours=1))
        assert risk.evaluate(_buy(t0, 2500, 2400, symbol="ETH/EUR")).approved

    async def test_daily_loss_blocks_until_next_day(self, t0: datetime) -> None:
        bus, clock, _, risk = _setup(t0)
        await _fill(bus, t0, Side.BUY, "0.001", "50000")  # 50 € en BTC
        await _fill(bus, t0, Side.SELL, "0.001", "46000")  # pierde 4 € (4 %) antes de la 1.ª señal
        assert "pérdida diaria" in risk.evaluate(_buy(t0)).reason
        clock.set(t0 + timedelta(minutes=5))
        await bus.publish(BarClosed(make_bars([46000], t0)[0]))  # marca de equity: 96 €
        assert "pérdida diaria" in risk.evaluate(_buy(t0)).reason  # sigue bloqueado hoy
        clock.set(t0 + timedelta(days=1))
        assert risk.evaluate(_buy(t0)).approved  # el día 2 parte de 96 €

    async def test_new_day_starts_from_previous_day_close(self, t0: datetime) -> None:
        """Una pérdida en los primeros minutos del día cuenta para el límite de ese día."""
        bus, clock, _, risk = _setup(t0)
        await _fill(bus, t0, Side.BUY, "0.001", "50000")
        for b in make_bars([50000, 50000], t0):  # marcas de equity del día 1
            await bus.publish(BarClosed(b))
        next_day = t0 + timedelta(days=1)
        clock.set(next_day)
        await _fill(bus, next_day, Side.SELL, "0.001", "46000")  # pérdida al abrir el día 2
        assert "pérdida diaria" in risk.evaluate(_buy(next_day)).reason

    async def test_losing_streak(self, t0: datetime) -> None:
        bus, _, _, risk = _setup(t0, repo_config(max_consecutive_losses=2, max_daily_loss_pct=20))
        for _ in range(2):
            await _fill(bus, t0, Side.BUY, "0.0001", "50000")
            await _fill(bus, t0, Side.SELL, "0.0001", "49900")
        assert "pérdidas seguidas" in risk.evaluate(_buy(t0)).reason

    async def test_paused_blocks_entries_but_allows_exits(self, t0: datetime) -> None:
        bus, _, _, risk = _setup(t0)
        await _fill(bus, t0, Side.BUY, "0.0004", "50000")
        risk.state = EngineState.PAUSED
        assert "paused" in risk.evaluate(_buy(t0, 2500, 2400, symbol="ETH/EUR")).reason
        assert risk.evaluate(_close(t0)).approved

    def test_spread_guard(self, t0: datetime) -> None:
        spreads = {"BTC/EUR": 0.01, "ETH/EUR": 0.9}
        _, _, _, risk = _setup(t0, spread_pct=lambda venue, symbol: spreads.get(symbol))
        assert risk.evaluate(_buy(t0)).approved
        assert "spread" in risk.evaluate(_buy(t0, 2500, 2400, symbol="ETH/EUR")).reason

    async def test_decisions_are_published(self, t0: datetime) -> None:
        bus, _, _, _ = _setup(t0)
        seen: list[RiskDecision] = []
        bus.subscribe(RiskDecision, seen.append)
        from invertio.core.events import SignalEmitted

        await bus.publish(SignalEmitted(_buy(t0)))
        assert len(seen) == 1 and seen[0].approved


def test_disabled_post_only_when_config_says_so(t0: datetime) -> None:
    config = with_venue(repo_config(prefer_post_only=False), "revolutx")
    _, _, _, risk = _setup(t0, config)
    order = risk.evaluate(_buy(t0)).order
    assert order is not None and order.type is OrderType.MARKET


@pytest.mark.parametrize("stop", [49_999.999])
def test_stop_rounding_keeps_stop_below_price(t0: datetime, stop: float) -> None:
    _, _, _, risk = _setup(t0)
    order = risk.evaluate(_buy(t0, price=50_000, stop=stop)).order
    assert order is not None and order.stop_loss == D("49999.99")
