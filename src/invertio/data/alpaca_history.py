"""Descarga de velas de acciones de EE. UU. desde Alpaca Market Data (plan gratuito, feed IEX).

Basta con una cuenta paper gratuita de Alpaca (disponible en cualquier país con un email).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx

from invertio.core.models import Bar
from invertio.core.timeframes import timeframe_seconds
from invertio.data.sessions import in_us_regular_session

DATA_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"
# El plan gratuito no sirve los últimos 15 minutos de datos consolidados.
FREE_PLAN_DELAY = timedelta(minutes=16)


def alpaca_timeframe(timeframe: str) -> str:
    seconds = timeframe_seconds(timeframe)
    if seconds % 86400 == 0:
        return f"{seconds // 86400}Day"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}Hour"
    return f"{seconds // 60}Min"


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


async def download_alpaca_bars(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    api_secret: str,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    venue: str,
    regular_session_only: bool = True,
    now: datetime | None = None,
    delay: timedelta = FREE_PLAN_DELAY,
    on_page: Callable[[int], None] | None = None,
) -> list[Bar]:
    """Descarga velas con `open_time` en [start, end). Por defecto solo la sesión regular.

    `delay`: margen respecto a ahora. Para históricos se usa el retraso del plan gratuito; en
    vivo, el feed IEX se pide sin retraso.
    """
    tf_delta = timedelta(seconds=timeframe_seconds(timeframe))
    now = now or datetime.now(UTC)
    end = min(end, now - delay)
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret}
    params: dict[str, str | int] = {
        "timeframe": alpaca_timeframe(timeframe),
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "limit": 10000,
        "feed": "iex",
        "adjustment": "all",  # ajustado por splits y dividendos
        "sort": "asc",
    }
    bars: list[Bar] = []
    while True:
        response = await client.get(
            DATA_URL.format(symbol=symbol), headers=headers, params=params, timeout=30
        )
        if response.status_code in (401, 403):
            raise PermissionError(
                "Alpaca rechazó las credenciales: revisa ALPACA_API_KEY y ALPACA_API_SECRET"
            )
        response.raise_for_status()
        payload = response.json()
        for raw in payload.get("bars") or []:
            open_time = _parse_ts(raw["t"])
            if open_time + tf_delta > now:
                continue  # vela aún abierta
            if regular_session_only and not in_us_regular_session(open_time):
                continue
            bars.append(
                Bar(
                    venue=venue,
                    symbol=symbol,
                    timeframe=timeframe,
                    open_time=open_time,
                    open=float(raw["o"]),
                    high=float(raw["h"]),
                    low=float(raw["l"]),
                    close=float(raw["c"]),
                    volume=float(raw["v"]),
                )
            )
        if on_page is not None:
            on_page(len(bars))
        token = payload.get("next_page_token")
        if not token:
            return bars
        params["page_token"] = token
