"""Cartera virtual independiente de órdenes reales y paper clásico."""

from typing import Any

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from invertio.persistence.models import Base


class SimulationStateRow(Base):
    __tablename__ = "simulation_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
