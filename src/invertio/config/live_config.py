"""Configuración del modo en vivo (paper/demo/live): config/live.yaml."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from invertio.config.app_config import AppConfig, read_yaml


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LiveMarket(_Strict):
    market: str  # venue:SÍMBOLO
    strategy: str

    @property
    def venue(self) -> str:
        return self.market.partition(":")[0]

    @property
    def symbol(self) -> str:
        return self.market.partition(":")[2]


class LiveConfig(_Strict):
    # Capital simulado del modo paper, en la moneda de cada venue.
    initial_cash: dict[str, Decimal]
    markets: list[LiveMarket] = Field(min_length=1)
    notify_rejections: bool = False  # avisar también de señales rechazadas por el riesgo
    stale_after_bars: int = Field(default=3, ge=2, le=50)  # velas sin datos → aviso

    @model_validator(mode="after")
    def _one_strategy_per_market(self) -> LiveConfig:
        seen = [m.market for m in self.markets]
        if len(seen) != len(set(seen)):
            raise ValueError("Cada mercado solo puede tener una estrategia asignada")
        return self

    def validate_against(self, config: AppConfig) -> None:
        """Comprueba que cada mercado existe en app.yaml y que su venue tiene capital."""
        for market in self.markets:
            venue = config.venue(market.venue)  # KeyError si no existe
            if market.symbol not in venue.symbols:
                raise ValueError(f"{market.market} no está en la lista de {venue.id} (app.yaml)")
            if venue.id not in self.initial_cash:
                raise ValueError(f"Falta initial_cash para {venue.id} en live.yaml")


def load_live_config(config_dir: Path, app_config: AppConfig) -> LiveConfig:
    live = LiveConfig.model_validate(read_yaml(config_dir / "live.yaml"))
    try:
        live.validate_against(app_config)
    except KeyError as exc:
        raise ValueError(f"live.yaml usa un venue desconocido: {exc}") from None
    return live
