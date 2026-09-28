"""Configuración del modo en vivo (paper/demo/live): config/live.yaml."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from invertio.config.app_config import AppConfig, InstrumentRules, read_yaml
from invertio.core.timeframes import timeframe_seconds


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


class UniverseConfig(_Strict):
    """Vigilar todos los pares al contado de un venue en su moneda (p. ej. todos en EUR)."""

    venue: str
    strategy: str
    exclude: list[str] = Field(default_factory=list)


class LiveConfig(_Strict):
    # Capital simulado del modo paper, en la moneda de cada venue.
    initial_cash: dict[str, Decimal]
    markets: list[LiveMarket] = Field(default_factory=list)
    universe: UniverseConfig | None = None
    timeframe: str | None = None  # si falta, se usa el de app.yaml (data.timeframe)
    notify_rejections: bool = False  # avisar también de señales rechazadas por el riesgo
    stale_after_bars: int = Field(default=3, ge=2, le=50)  # velas sin datos → aviso

    @model_validator(mode="after")
    def _check(self) -> LiveConfig:
        seen = [m.market for m in self.markets]
        if len(seen) != len(set(seen)):
            raise ValueError("Cada mercado solo puede tener una estrategia asignada")
        if not self.markets and self.universe is None:
            raise ValueError("live.yaml necesita `markets` o `universe`")
        if self.timeframe is not None:
            timeframe_seconds(self.timeframe)
        return self

    def validate_against(self, config: AppConfig) -> None:
        """Comprueba que cada mercado existe en app.yaml y que su venue tiene capital."""
        for market in self.markets:
            venue = config.venue(market.venue)  # KeyError si no existe
            if market.symbol not in venue.symbols:
                raise ValueError(f"{market.market} no está en la lista de {venue.id} (app.yaml)")
            if venue.id not in self.initial_cash:
                raise ValueError(f"Falta initial_cash para {venue.id} en live.yaml")
        if self.universe is not None:
            venue = config.venue(self.universe.venue)
            if venue.kind != "ccxt":
                raise ValueError("`universe` solo está disponible para venues de cripto (ccxt)")
            if venue.id not in self.initial_cash:
                raise ValueError(f"Falta initial_cash para {venue.id} en live.yaml")


def expand_universe(
    config: AppConfig,
    live: LiveConfig,
    symbols: list[str],
    rules: Mapping[str, InstrumentRules],
) -> tuple[AppConfig, LiveConfig]:
    """Añade los `symbols` del universo como mercados vigilados y a la lista blanca del venue,
    con las reglas de precisión reales de cada uno (`rules`, leídas del exchange)."""
    universe = live.universe
    if universe is None:
        return config, live
    listed = {m.market for m in live.markets}
    excluded = set(universe.exclude)
    added = [
        LiveMarket(market=f"{universe.venue}:{s}", strategy=universe.strategy)
        for s in symbols
        if s not in excluded and f"{universe.venue}:{s}" not in listed
    ]
    venues = []
    for venue in config.venues:
        if venue.id == universe.venue:
            whitelist = list(dict.fromkeys([*venue.symbols, *(m.symbol for m in added)]))
            overrides = {**venue.instrument_overrides, **rules}
            venue = venue.model_copy(
                update={"symbols": whitelist, "instrument_overrides": overrides}
            )
        venues.append(venue)
    return (
        config.model_copy(update={"venues": venues}),
        live.model_copy(update={"markets": [*live.markets, *added]}),
    )


def load_live_config(config_dir: Path, app_config: AppConfig) -> LiveConfig:
    live = LiveConfig.model_validate(read_yaml(config_dir / "live.yaml"))
    try:
        live.validate_against(app_config)
    except KeyError as exc:
        raise ValueError(f"live.yaml usa un venue desconocido: {exc}") from None
    return live
