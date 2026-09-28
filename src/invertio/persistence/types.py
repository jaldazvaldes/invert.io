"""Tipos de columna para SQLite.

- `DecimalString`: SQLite no tiene decimal exacto, así que se guarda como texto para no
  perder precisión.
- `UTCDateTime`: SQLite pierde la zona horaria, así que se guarda en UTC y se devuelve
  como datetime aware en UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Dialect, String
from sqlalchemy.types import TypeDecorator


class DecimalString(TypeDecorator[Decimal]):
    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, Decimal):
            raise TypeError(f"Se esperaba Decimal, llegó {type(value).__name__}")
        return format(value, "f")

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        return None if value is None else Decimal(value)


class UTCDateTime(TypeDecorator[datetime]):
    impl = DateTime()
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError(f"Solo se guardan datetimes en UTC con zona horaria: {value!r}")
        return value.replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)
