"""Configuración YAML: venues (config/app.yaml) y límites de riesgo (config/risk.yaml)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from invertio.core.models import AssetClass, Instrument
from invertio.core.timeframes import timeframe_seconds


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FeesConfig(_Strict):
    """Comisiones en porcentaje (0.09 = 0,09 %)."""

    maker_pct: float = Field(ge=0, le=5)
    taker_pct: float = Field(ge=0, le=5)
    fx_pct: float = Field(default=0, ge=0, le=5)  # cambio de divisa por lado (Trading 212 en USD)


class SimulationConfig(_Strict):
    """Supuestos del simulador (backtest y paper). Porcentajes sobre el precio."""

    spread_pct: float = Field(ge=0, le=5)  # spread completo; se paga la mitad al entrar y al salir
    slippage_pct: float = Field(ge=0, le=5)  # deslizamiento extra en órdenes a mercado y stops
    limit_ttl_bars: int = Field(default=2, ge=1, le=100)  # velas que espera una orden límite


class InstrumentRules(_Strict):
    """Reglas de precisión y mínimos. El adaptador real las lee del bróker en la fase 4."""

    price_step: Decimal = Field(gt=0)
    amount_step: Decimal = Field(gt=0)
    min_amount: Decimal = Field(default=Decimal(0), ge=0)
    min_notional: Decimal = Field(default=Decimal(0), ge=0)


class VenueConfig(_Strict):
    id: str
    kind: Literal["ccxt", "trading212"]
    asset_class: AssetClass
    enabled: bool = True
    quote_currency: str
    symbols: list[str] = Field(min_length=1)
    supports_post_only: bool = False
    fees: FeesConfig
    simulation: SimulationConfig
    instrument: InstrumentRules
    instrument_overrides: dict[str, InstrumentRules] = Field(default_factory=dict)

    def instrument_for(self, symbol: str) -> Instrument:
        rules = self.instrument_overrides.get(symbol, self.instrument)
        base = symbol.split("/")[0] if "/" in symbol else symbol
        return Instrument(
            venue=self.id,
            symbol=symbol,
            asset_class=self.asset_class,
            base=base,
            quote=self.quote_currency,
            price_step=rules.price_step,
            amount_step=rules.amount_step,
            min_amount=rules.min_amount,
            min_notional=rules.min_notional,
        )


class DataConfig(_Strict):
    timeframe: str = "5m"
    stocks_provider: Literal["alpaca"] = "alpaca"
    # Exchange (id de ccxt) del que se descargan históricos de cripto. Revolut X solo guarda
    # ~4 semanas de velas de 5m; OKX EEA (myokx) guarda más de 2 años y sus datos públicos
    # no requieren cuenta.
    crypto_history_source: str = "myokx"

    @model_validator(mode="after")
    def _check_timeframe(self) -> DataConfig:
        seconds = timeframe_seconds(self.timeframe)
        if not 60 <= seconds <= 86400:
            raise ValueError("El timeframe debe estar entre 1m y 1d")
        return self


class RiskConfig(_Strict):
    """Límites duros. Los porcentajes son del capital (equity) de cada venue."""

    max_risk_per_trade_pct: float = Field(gt=0, le=5)
    max_position_pct: float = Field(gt=0, le=100)
    max_open_positions: int = Field(ge=1, le=20)
    max_daily_loss_pct: float = Field(gt=0, le=20)
    max_orders_per_hour: int = Field(ge=1, le=500)
    max_consecutive_losses: int = Field(ge=1, le=20)
    max_spread_pct: float = Field(gt=0, le=5)
    prefer_post_only: bool = True
    # Zona horaria que define "el día" para el límite de pérdida diaria.
    day_timezone: str = "Europe/Madrid"

    @field_validator("day_timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Zona horaria desconocida: {value}") from exc
        return value


class AppConfig(_Strict):
    venues: list[VenueConfig]
    data: DataConfig = DataConfig()
    risk: RiskConfig

    @model_validator(mode="after")
    def _unique_venues(self) -> AppConfig:
        ids = [v.id for v in self.venues]
        if len(ids) != len(set(ids)):
            raise ValueError(f"Hay venues con id repetido: {ids}")
        return self

    def venue(self, venue_id: str) -> VenueConfig:
        for venue in self.venues:
            if venue.id == venue_id:
                return venue
        raise KeyError(f"Venue desconocido: {venue_id}")


def read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"No existe el fichero de configuración {path}")
    content = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(content, dict):
        raise ValueError(f"{path} debe contener un mapa YAML")
    return content


def load_app_config(config_dir: Path) -> AppConfig:
    app = read_yaml(config_dir / "app.yaml")
    risk = read_yaml(config_dir / "risk.yaml")
    return AppConfig.model_validate({**app, "risk": risk})
