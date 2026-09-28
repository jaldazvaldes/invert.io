"""Reloj inyectable: en vivo es la hora real y en backtest avanza con las velas.

Todo lo que dependa de la hora (límite de pérdida diaria, horario de mercado, etc.) debe
usar un `Clock` y nunca `datetime.now()` directamente. Si no, el backtest y el modo en vivo
se comportarían distinto.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class LiveClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SimClock:
    def __init__(self, start: datetime) -> None:
        self._check_utc(start)
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, ts: datetime) -> None:
        self._check_utc(ts)
        if ts < self._now:
            raise ValueError(f"El reloj simulado no puede retroceder ({self._now} → {ts})")
        self._now = ts

    @staticmethod
    def _check_utc(ts: datetime) -> None:
        if ts.tzinfo is None or ts.utcoffset() != timedelta(0):
            raise ValueError("El reloj trabaja en UTC con zona horaria")
