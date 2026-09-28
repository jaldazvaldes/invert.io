"""Feeds de datos en vivo: velas cerradas por sondeo y spread actual.

Revolut X no ofrece websockets, así que se sondea por REST justo después de cada cierre de
vela. Con velas de 1 a 15 minutos es más que suficiente.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Protocol

import httpx

from invertio.core.clock import Clock
from invertio.core.models import Bar
from invertio.data.alpaca_history import download_alpaca_bars
from invertio.data.ccxt_history import create_exchange, download_ohlcv

STABLECOINS = frozenset(
    {"USDC", "USDT", "DAI", "EURC", "EURT", "PYUSD", "FDUSD", "USDE", "RLUSD", "TUSD", "USDG"}
)


class LiveFeed(Protocol):
    venue: str
    has_live_spread: bool

    async def closed_bars(
        self, symbol: str, timeframe: str, since: datetime, now: datetime
    ) -> list[Bar]:
        """Velas cerradas con `open_time` >= `since`."""
        ...

    async def spread_pct(self, symbol: str) -> float | None: ...

    async def close(self) -> None: ...


class CcxtLiveFeed:
    """Velas y libro de órdenes públicos de un exchange ccxt (sin claves)."""

    has_live_spread = True

    def __init__(self, venue: str, exchange_id: str, exchange: Any | None = None) -> None:
        self.venue = venue
        self._exchange = exchange if exchange is not None else create_exchange(exchange_id)

    async def closed_bars(
        self, symbol: str, timeframe: str, since: datetime, now: datetime
    ) -> list[Bar]:
        return await download_ohlcv(
            self._exchange, symbol, timeframe, since, now, venue=self.venue, now=now
        )

    async def spread_pct(self, symbol: str) -> float | None:
        book = await self._exchange.fetch_order_book(symbol, limit=5)
        if not book.get("bids") or not book.get("asks"):
            return None
        bid, ask = float(book["bids"][0][0]), float(book["asks"][0][0])
        mid = (bid + ask) / 2
        return (ask - bid) / mid * 100 if mid > 0 else None

    async def symbols(self, quote: str) -> list[str]:
        """Mercados al contado activos que cotizan en `quote` (p. ej. todos los pares en EUR),
        sin stablecoins: su precio no se mueve y no tiene sentido puntuarlas."""
        markets = await self._exchange.load_markets()
        return sorted(
            symbol
            for symbol, market in markets.items()
            if market.get("quote") == quote
            and market.get("spot")
            and market.get("active", True)
            and market.get("base") not in STABLECOINS
        )

    async def close(self) -> None:
        await self._exchange.close()


class AlpacaLiveFeed:
    """Velas de acciones de EE. UU. (feed IEX gratuito de Alpaca), solo sesión regular.

    Las cotizaciones de IEX (un único mercado) no reflejan el spread real del mercado, así que
    este feed no da spread y el riesgo usa el supuesto de config/app.yaml.
    """

    has_live_spread = False

    def __init__(self, venue: str, api_key: str, api_secret: str) -> None:
        self.venue = venue
        self._key = api_key
        self._secret = api_secret
        self._client = httpx.AsyncClient()

    async def closed_bars(
        self, symbol: str, timeframe: str, since: datetime, now: datetime
    ) -> list[Bar]:
        return await download_alpaca_bars(
            self._client,
            api_key=self._key,
            api_secret=self._secret,
            symbol=symbol,
            timeframe=timeframe,
            start=since,
            end=now,
            venue=self.venue,
            now=now,
            delay=timedelta(0),
        )

    async def spread_pct(self, symbol: str) -> float | None:
        return None

    async def close(self) -> None:
        await self._client.aclose()


class SpreadBook:
    """Último spread conocido de cada mercado, para el control de riesgo (que es síncrono).

    Si un venue no da spread en vivo, se usa el supuesto de la configuración. Un spread
    antiguo se considera desconocido: el riesgo no deja entrar a ciegas.
    """

    def __init__(
        self, clock: Clock, fallback_pct: dict[str, float | None], max_age: timedelta
    ) -> None:
        self._clock = clock
        self._fallback = fallback_pct
        self._max_age = max_age
        self._values: dict[tuple[str, str], tuple[float, datetime]] = {}

    def update(self, venue: str, symbol: str, spread_pct: float) -> None:
        self._values[(venue, symbol)] = (spread_pct, self._clock.now())

    def get(self, venue: str, symbol: str) -> float | None:
        fallback = self._fallback.get(venue)
        if fallback is not None:
            return fallback
        value = self._values.get((venue, symbol))
        if value is None or self._clock.now() - value[1] > self._max_age:
            return None
        return value[0]
