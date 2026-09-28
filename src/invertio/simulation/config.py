"""Parámetros independientes de la simulación automática."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SimulationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    initial_eur: Decimal = Field(default=Decimal("50"), gt=0)
    allocation_eur: Decimal = Field(default=Decimal("10"), gt=0)
    max_positions: int = Field(default=5, ge=1, le=20)
    fee_pct: Decimal = Field(default=Decimal("0.09"), ge=0, lt=100)
    quote_max_age_seconds: int = Field(default=60, ge=1)
    bar_max_age_seconds: int = Field(default=120, ge=1)
    entry_score: Decimal = Field(default=Decimal("70"), gt=0, le=100)
    exit_score: Decimal = Field(default=Decimal("40"), ge=0, lt=100)
    stop_atr: Decimal = Field(default=Decimal("2"), gt=0)
    target_atr: Decimal = Field(default=Decimal("3"), gt=0)
    lifetime_minutes: int = Field(default=240, ge=1)
    max_slippage_pct: Decimal = Field(default=Decimal("0.1"), ge=0, lt=100)
    max_spread_pct: Decimal = Field(default=Decimal("0.3"), ge=0, lt=100)

    @model_validator(mode="after")
    def _consistent(self) -> SimulationConfig:
        if self.allocation_eur > self.initial_eur:
            raise ValueError("La asignación no puede superar el capital inicial")
        if self.exit_score >= self.entry_score:
            raise ValueError("La nota de salida debe ser menor que la de entrada")
        return self
