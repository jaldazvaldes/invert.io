"""Registro duradero de previsualizaciones y órdenes reales manuales."""

from typing import Any

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from invertio.persistence.models import Base


class ManualOrderRow(Base):
    __tablename__ = "manual_orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
