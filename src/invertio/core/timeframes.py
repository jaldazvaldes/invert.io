"""Timeframes de velas ("1m", "5m", "15m", "1h", "1d")."""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import lru_cache

_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}


@lru_cache(maxsize=64)
def timeframe_seconds(timeframe: str) -> int:
    """Duración de un timeframe en segundos. Lanza ValueError si el formato no es válido."""
    if len(timeframe) < 2 or timeframe[-1] not in _UNIT_SECONDS or not timeframe[:-1].isdigit():
        raise ValueError(f"Timeframe no válido: {timeframe!r} (ejemplos: '1m', '5m', '1h', '1d')")
    amount = int(timeframe[:-1])
    if amount <= 0:
        raise ValueError(f"Timeframe no válido: {timeframe!r}")
    return amount * _UNIT_SECONDS[timeframe[-1]]


def timeframe_delta(timeframe: str) -> timedelta:
    return timedelta(seconds=timeframe_seconds(timeframe))


def floor_to_timeframe(ts: datetime, timeframe: str) -> datetime:
    """Apertura de la vela que contiene `ts` (velas alineadas a la época Unix, en UTC)."""
    step = timeframe_seconds(timeframe)
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - epoch % step, tz=ts.tzinfo)
