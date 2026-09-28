from datetime import datetime
from decimal import Decimal
from typing import Any

from ccxt.base.decimal_to_precision import TICK_SIZE

from invertio.config.live_config import LiveConfig, LiveMarket, UniverseConfig, expand_universe
from invertio.core.bus import EventBus
from invertio.core.clock import SimClock
from invertio.core.models import Signal, SignalAction
from invertio.live.feeds import CcxtLiveFeed
from invertio.portfolio import Portfolio
from invertio.risk import RiskManager
from tests.helpers import repo_config

D = Decimal


class FakeExchange:
    precisionMode = TICK_SIZE

    async def load_markets(self) -> dict[str, Any]:
        def market(base: str, price: float, amount: float, **extra: Any) -> dict[str, Any]:
            return {
                "base": base,
                "quote": "EUR",
                "spot": True,
                "active": True,
                "precision": {"price": price, "amount": amount},
                "limits": {"amount": {"min": amount}, "cost": {"min": 0.1}},
                **extra,
            }

        return {
            "BTC/EUR": market("BTC", 0.01, 1e-8),
            "BONK/EUR": market("BONK", 1e-8, 1.0),
            "USDC/EUR": market("USDC", 0.0001, 0.01),
            "OLD/EUR": market("OLD", 0.01, 0.01, active=False),
            "BTC/USD": {**market("BTC", 0.01, 1e-8), "quote": "USD"},
        }

    async def fetch_tickers(self, symbols: list[str]) -> dict[str, Any]:
        return {
            "BTC/EUR": {"bid": 99.99, "ask": 100.01},
            "BONK/EUR": {"bid": None, "ask": 0.00002},
        }

    async def close(self) -> None:
        pass


async def test_feed_lists_markets_rules_and_spreads() -> None:
    feed = CcxtLiveFeed("revolutx", "revolutx", exchange=FakeExchange())
    assert await feed.symbols("EUR") == ["BONK/EUR", "BTC/EUR"]  # sin stablecoins ni inactivos
    rules = await feed.instrument_rules(["BTC/EUR", "BONK/EUR", "NOPE/EUR"])
    assert rules["BONK/EUR"].price_step == D("1E-8")
    assert rules["BTC/EUR"].amount_step == D("1E-8")
    assert "NOPE/EUR" not in rules
    spreads = await feed.spreads(["BTC/EUR", "BONK/EUR"])
    assert set(spreads) == {"BTC/EUR"}  # sin bid no hay spread
    assert abs(spreads["BTC/EUR"] - 0.02) < 1e-9


async def test_expand_universe_whitelists_and_uses_real_precision(t0: datetime) -> None:
    config = repo_config()
    live = LiveConfig(
        initial_cash={"revolutx": D(100)},
        markets=[LiveMarket(market="revolutx:BTC/EUR", strategy="ema_cross")],
        universe=UniverseConfig(venue="revolutx", strategy="puntuacion", exclude=["DOGE/EUR"]),
    )
    feed = CcxtLiveFeed("revolutx", "revolutx", exchange=FakeExchange())
    symbols = ["BTC/EUR", "BONK/EUR", "DOGE/EUR"]
    rules = await feed.instrument_rules(symbols)
    config, live = expand_universe(config, live, symbols, rules)

    assert [(m.market, m.strategy) for m in live.markets] == [
        ("revolutx:BTC/EUR", "ema_cross"),  # la asignación explícita se respeta
        ("revolutx:BONK/EUR", "puntuacion"),
    ]
    venue = config.venue("revolutx")
    assert "BONK/EUR" in venue.symbols and "DOGE/EUR" not in venue.symbols
    live.validate_against(config)

    # El gestor de riesgo acepta BONK y respeta su precisión real (no redondea el stop a 0).
    bus = EventBus(strict=True)
    portfolio = Portfolio(bus, {"revolutx": D(100)}, {"revolutx": "EUR"})
    risk = RiskManager(bus, SimClock(t0), config, portfolio)
    signal = Signal(
        "s", "revolutx", "BONK/EUR", SignalAction.BUY, t0, 0.00001234, stop_loss=0.0000118
    )
    decision = risk.evaluate(signal)
    assert decision.approved, decision.reason
    assert decision.order is not None and decision.order.stop_loss == D("0.00001180")
