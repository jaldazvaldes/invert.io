"""Datos del laboratorio: históricos de muchos mercados y remuestreo a timeframes mayores."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from invertio.core.models import Bar
from invertio.core.timeframes import floor_to_timeframe, timeframe_delta
from invertio.data.ccxt_history import create_exchange, download_ohlcv
from invertio.data.store import BarStore


async def common_symbols(venue_id: str, source_id: str, quote: str) -> list[str]:
    """Pares en `quote` que existen en el venue donde se opera y en la fuente de históricos."""
    from invertio.live.feeds import CcxtLiveFeed

    venue_feed = CcxtLiveFeed(venue_id, venue_id)
    source = create_exchange(source_id)
    try:
        venue_symbols = set(await venue_feed.symbols(quote))
        markets = await source.load_markets()
        source_symbols = {
            s for s, m in markets.items() if m.get("quote") == quote and m.get("spot")
        }
    finally:
        await venue_feed.close()
        await source.close()
    return sorted(venue_symbols & source_symbols)


async def download_many(
    source_id: str,
    symbols: list[str],
    timeframe: str,
    days: int,
    store: BarStore,
    *,
    venue: str,
    on_symbol: Callable[[str, int, int], None] | None = None,
    full: bool = False,
) -> dict[str, int]:
    """Descarga (incremental) `days` días de velas de cada símbolo. Devuelve velas guardadas.

    Lo ya guardado se da por completo desde su primera vela: muchas monedas empezaron a
    cotizar después del inicio de la ventana y no hay historia anterior que pedir. Con
    `full=True` se vuelve a pedir toda la ventana (p. ej. tras ampliar `history_days`).
    """
    exchange = create_exchange(source_id)
    now = datetime.now(UTC)
    start = now - timedelta(days=days)
    totals = {}
    try:
        for index, symbol in enumerate(symbols, start=1):
            info = store.info(source_id, symbol, timeframe)
            first = start
            if info is not None and not full:
                first = max(start, info.last + timeframe_delta(timeframe))
            bars = await download_ohlcv(
                exchange, symbol, timeframe, first, now, venue=venue, now=now
            )
            totals[symbol] = (
                store.write(source_id, symbol, timeframe, bars)
                if bars
                else (info.rows if info else 0)
            )
            if on_symbol is not None:
                on_symbol(symbol, index, len(symbols))
    finally:
        await exchange.close()
    return totals


def resample(bars: list[Bar], timeframe: str) -> list[Bar]:
    """Agrupa velas en otras más largas (p. ej. 1h → 4h). Solo devuelve velas completas."""
    if not bars:
        return []
    target = timeframe_delta(timeframe)
    source = timeframe_delta(bars[0].timeframe)
    if target <= source or target % source:
        raise ValueError(f"No se puede pasar de {bars[0].timeframe} a {timeframe}")
    per_bucket = target // source
    result = []
    bucket: list[Bar] = []
    for bar in bars:
        if bucket and floor_to_timeframe(bar.open_time, timeframe) != floor_to_timeframe(
            bucket[0].open_time, timeframe
        ):
            if len(bucket) == per_bucket:
                result.append(_merge(bucket, timeframe))
            bucket = []
        bucket.append(bar)
    if len(bucket) == per_bucket:
        result.append(_merge(bucket, timeframe))
    return result


def _merge(bucket: list[Bar], timeframe: str) -> Bar:
    first = bucket[0]
    return Bar(
        venue=first.venue,
        symbol=first.symbol,
        timeframe=timeframe,
        open_time=floor_to_timeframe(first.open_time, timeframe),
        open=first.open,
        high=max(b.high for b in bucket),
        low=min(b.low for b in bucket),
        close=bucket[-1].close,
        volume=sum(b.volume for b in bucket),
    )
