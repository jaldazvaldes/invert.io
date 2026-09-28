"""Descarga de velas históricas de cripto vía ccxt (datos públicos, sin claves)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog

from invertio.core.models import Bar
from invertio.core.timeframes import timeframe_seconds

log = structlog.get_logger(__name__)

# Velas máximas por petición. Revolut X rechaza ventanas de más de 1000 velas.
_PAGE_LIMITS = {"revolutx": 1000, "okx": 300, "myokx": 300}
_DEFAULT_PAGE_LIMIT = 500


class OhlcvExchange(Protocol):
    """Lo que usamos de un exchange async de ccxt (permite simularlo en los tests)."""

    id: str

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int | None = None,
        limit: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[list[Any]]: ...


def create_exchange(exchange_id: str) -> Any:
    import ccxt.async_support as ccxt_async

    if exchange_id not in ccxt_async.exchanges:
        raise ValueError(f"ccxt no conoce el exchange {exchange_id!r}")
    return getattr(ccxt_async, exchange_id)({"enableRateLimit": True})


async def download_ohlcv(
    exchange: OhlcvExchange,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    *,
    venue: str,
    now: datetime | None = None,
    on_page: Callable[[int, datetime], None] | None = None,
) -> list[Bar]:
    """Descarga velas cerradas con `open_time` en [start, end), paginando por ventanas.

    Solo devuelve velas ya cerradas (su cierre es anterior a `now`).
    """
    tf_ms = timeframe_seconds(timeframe) * 1000
    page_limit = _PAGE_LIMITS.get(exchange.id, _DEFAULT_PAGE_LIMIT)
    now = now or datetime.now(UTC)
    end = min(end, now)
    cursor = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    now_ms = int(now.timestamp() * 1000)
    bars: dict[int, Bar] = {}

    while cursor < end_ms:
        window_end = min(cursor + page_limit * tf_ms, end_ms)
        rows = await exchange.fetch_ohlcv(
            symbol,
            timeframe,
            since=cursor,
            limit=page_limit,
            params={"until": window_end - 1},
        )
        for ts, o, h, low, c, v in (row[:6] for row in rows):
            ts = int(ts)
            if not cursor <= ts < window_end or ts + tf_ms > now_ms:
                continue
            bars[ts] = Bar(
                venue=venue,
                symbol=symbol,
                timeframe=timeframe,
                open_time=datetime.fromtimestamp(ts / 1000, UTC),
                open=float(o),
                high=float(h),
                low=float(low),
                close=float(c),
                volume=float(v or 0),
            )
        # Siempre se avanza la ventana completa: los huecos sin operaciones no tienen vela.
        cursor = window_end
        if on_page is not None:
            on_page(len(bars), datetime.fromtimestamp(cursor / 1000, UTC))

    log.debug("velas descargadas", exchange=exchange.id, symbol=symbol, count=len(bars))
    return [bars[ts] for ts in sorted(bars)]
