"""Orquestación de descargas: qué fuente usar para cada mercado y descarga incremental."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import httpx

from invertio.config.app_config import AppConfig, VenueConfig
from invertio.config.settings import Settings
from invertio.core.models import AssetClass
from invertio.core.timeframes import timeframe_delta
from invertio.data.alpaca_history import download_alpaca_bars
from invertio.data.ccxt_history import create_exchange, download_ohlcv
from invertio.data.store import BarStore

ALPACA = "alpaca"


@dataclass(frozen=True, slots=True)
class Market:
    venue: VenueConfig
    symbol: str

    @property
    def label(self) -> str:
        return f"{self.venue.id}:{self.symbol}"


def parse_market(config: AppConfig, text: str) -> Market:
    """'revolutx:BTC/EUR' → Market. El símbolo debe estar en la lista blanca del venue."""
    venue_id, sep, symbol = text.partition(":")
    if not sep or not symbol:
        raise ValueError(f"Mercado {text!r} no válido: usa venue:SÍMBOLO (p. ej. revolutx:BTC/EUR)")
    try:
        venue = config.venue(venue_id)
    except KeyError:
        known = ", ".join(v.id for v in config.venues)
        raise ValueError(f"Venue desconocido {venue_id!r}. Configurados: {known}") from None
    if symbol not in venue.symbols:
        raise ValueError(
            f"{symbol} no está en la lista de {venue.id} ({', '.join(venue.symbols)}). "
            "Añádelo en config/app.yaml"
        )
    return Market(venue, symbol)


def history_source(config: AppConfig, venue: VenueConfig) -> str:
    if venue.asset_class is AssetClass.STOCK:
        return ALPACA
    return config.data.crypto_history_source


async def download_market(
    settings: Settings,
    config: AppConfig,
    store: BarStore,
    market: Market,
    timeframe: str,
    start: datetime,
    end: datetime,
    *,
    on_progress: Callable[[int], None] | None = None,
) -> tuple[str, int, int]:
    """Descarga y guarda velas. Si ya hay datos desde `start`, solo baja lo que falta.

    Devuelve (fuente, velas descargadas, total guardado).
    """
    source = history_source(config, market.venue)
    existing = store.info(source, market.symbol, timeframe)
    first_needed = start
    if existing is not None and existing.first <= start:
        first_needed = max(start, existing.last + timeframe_delta(timeframe))
    if first_needed >= end:
        return source, 0, existing.rows if existing else 0

    if source == ALPACA:
        if settings.alpaca_api_key is None or settings.alpaca_api_secret is None:
            raise PermissionError(
                "Para descargar acciones hace falta una cuenta paper gratuita de Alpaca: "
                "pon ALPACA_API_KEY y ALPACA_API_SECRET en .env"
            )
        async with httpx.AsyncClient() as client:
            bars = await download_alpaca_bars(
                client,
                api_key=settings.alpaca_api_key.get_secret_value(),
                api_secret=settings.alpaca_api_secret.get_secret_value(),
                symbol=market.symbol,
                timeframe=timeframe,
                start=first_needed,
                end=end,
                venue=market.venue.id,
                on_page=on_progress,
            )
    else:
        exchange = create_exchange(source)
        try:
            bars = await download_ohlcv(
                exchange,
                market.symbol,
                timeframe,
                first_needed,
                end,
                venue=market.venue.id,
                on_page=(lambda n, _ts: on_progress(n)) if on_progress else None,
            )
        finally:
            await exchange.close()

    total = store.write(source, market.symbol, timeframe, bars) if bars else 0
    return source, len(bars), total
