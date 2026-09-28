"""Esquema de la base de datos.

Cada fila guarda el `mode` (paper/demo/live) para que los datos de prueba nunca se mezclen
con los reales. Los backtests no escriben aquí: sus resultados van a ficheros de informe.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from invertio.persistence.types import DecimalString, UTCDateTime


class Base(DeclarativeBase):
    pass


class SignalRow(Base):
    """Cada señal emitida y la decisión del gestor de riesgo sobre ella."""

    __tablename__ = "signals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    mode: Mapped[str] = mapped_column(String(10))
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    strategy_id: Mapped[str] = mapped_column(String(64))
    venue: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(10))
    price: Mapped[float]
    stop_loss: Mapped[float | None]
    take_profit: Mapped[float | None]
    reason: Mapped[str] = mapped_column(Text, default="")
    approved: Mapped[bool | None]  # None = pendiente de decisión
    decision_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_signals_mode_ts", "mode", "ts"),)


class OrderRow(Base):
    __tablename__ = "orders"

    client_order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mode: Mapped[str] = mapped_column(String(10))
    venue: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(4))
    type: Mapped[str] = mapped_column(String(10))
    quantity: Mapped[Decimal] = mapped_column(DecimalString)
    limit_price: Mapped[Decimal | None] = mapped_column(DecimalString)
    post_only: Mapped[bool]
    stop_loss: Mapped[Decimal | None] = mapped_column(DecimalString)
    take_profit: Mapped[Decimal | None] = mapped_column(DecimalString)
    reason: Mapped[str] = mapped_column(String(32), default="")
    strategy_id: Mapped[str] = mapped_column(String(64))
    signal_id: Mapped[str | None] = mapped_column(ForeignKey("signals.id"))
    venue_order_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(20))
    filled_quantity: Mapped[Decimal] = mapped_column(DecimalString)
    avg_fill_price: Mapped[Decimal | None] = mapped_column(DecimalString)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        Index("ix_orders_mode_created", "mode", "created_at"),
        Index("ix_orders_status", "status"),
    )


class FillRow(Base):
    __tablename__ = "fills"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_order_id: Mapped[str] = mapped_column(ForeignKey("orders.client_order_id"))
    mode: Mapped[str] = mapped_column(String(10))
    venue: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[Decimal] = mapped_column(DecimalString)
    price: Mapped[Decimal] = mapped_column(DecimalString)
    fee: Mapped[Decimal] = mapped_column(DecimalString)
    fee_currency: Mapped[str] = mapped_column(String(10))
    ts: Mapped[datetime] = mapped_column(UTCDateTime)

    __table_args__ = (Index("ix_fills_mode_ts", "mode", "ts"),)


class EquitySnapshotRow(Base):
    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(10))
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    venue: Mapped[str] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(10))
    equity: Mapped[Decimal] = mapped_column(DecimalString)
    cash: Mapped[Decimal] = mapped_column(DecimalString)

    __table_args__ = (Index("ix_equity_mode_venue_ts", "mode", "venue", "ts"),)


class AuditLogRow(Base):
    """Registro de sucesos relevantes: cambios de estado del motor, pánico, errores, etc."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(10))
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    level: Mapped[str] = mapped_column(String(10))
    event: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    __table_args__ = (Index("ix_audit_mode_ts", "mode", "ts"),)
