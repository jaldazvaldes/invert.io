"""Configuración del laboratorio (config/lab.yaml)."""

from __future__ import annotations

import itertools
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from invertio.config.app_config import read_yaml
from invertio.core.timeframes import timeframe_seconds
from invertio.strategies import STRATEGIES


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VerdictConfig(_Strict):
    min_median_return_pct: float = 0
    min_positive_pct: float = Field(default=55, ge=0, le=100)
    min_avg_trades: float = Field(default=5, ge=0)


BTC_FILTER = "btc_filter"


def _no_filter() -> list[int | None]:
    return [None]


def split_filter(params: dict[str, Any]) -> tuple[dict[str, Any], int | None]:
    """Separa el filtro de BTC (opción del laboratorio) de los parámetros de la estrategia."""
    rest = dict(params)
    return rest, rest.pop(BTC_FILTER, None)


class StrategyGrid(_Strict):
    timeframes: list[str] = Field(min_length=1)
    grid: dict[str, list[Any]] = Field(default_factory=dict)
    # Días de la media de BTC para permitir compras (null = sin filtro). Ver strategies/regime.
    btc_filter: list[int | None] = Field(default_factory=_no_filter, min_length=1)

    @field_validator("btc_filter")
    @classmethod
    def _valid_filter(cls, value: list[int | None]) -> list[int | None]:
        if any(days is not None and days < 2 for days in value):
            raise ValueError("btc_filter necesita medias de 2 días o más")
        return value

    @field_validator("timeframes")
    @classmethod
    def _valid_timeframes(cls, value: list[str]) -> list[str]:
        for tf in value:
            timeframe_seconds(tf)
        return value


class RotationVerdict(_Strict):
    """Aprueba si el retorno supera el mínimo en entrenamiento y en test y, en test, además
    supera a mantener BTC (si `beat_btc`) con operaciones suficientes."""

    min_return_pct: float = 0
    beat_btc: bool = True  # más rentable que comprar y mantener BTC en el mismo periodo
    min_trades: int = Field(default=20, ge=0)  # compras + ventas en el periodo


class RotationConfig(_Strict):
    """Rotación semanal por momento entre todas las monedas (ver lab/rotation.py)."""

    lookback_days: list[int] = Field(min_length=1)
    top: list[int] = Field(min_length=1)
    btc_filter: list[int | None] = Field(default_factory=_no_filter, min_length=1)
    rebalance_days: int = Field(default=7, ge=1)
    fee_pct: float = Field(default=0.09, ge=0)  # compras y ventas a mercado (taker)
    verdict: RotationVerdict = RotationVerdict()

    def combinations(self) -> list[dict[str, Any]]:
        return [
            {"lookback_days": lookback, "top": top, "btc_filter": days}
            for lookback, top, days in itertools.product(
                self.lookback_days, self.top, self.btc_filter
            )
        ]


class LabConfig(_Strict):
    source: str
    venue: str
    base_timeframe: str = "1h"
    history_days: int = Field(default=1095, ge=90)
    test_days: int = Field(default=365, ge=30)
    min_train_days: int = Field(default=180, ge=10)
    min_test_days: int = Field(default=180, ge=10)
    capital: Decimal = Decimal(100)
    verdict: VerdictConfig = VerdictConfig()
    strategies: dict[str, StrategyGrid]
    rotation: RotationConfig | None = None

    @field_validator("strategies")
    @classmethod
    def _known(cls, value: dict[str, StrategyGrid]) -> dict[str, StrategyGrid]:
        unknown = set(value) - set(STRATEGIES)
        if unknown:
            raise ValueError(f"Estrategias desconocidas en lab.yaml: {sorted(unknown)}")
        return value

    def combinations(self, strategy_id: str) -> list[dict[str, Any]]:
        """Combinaciones válidas de parámetros (las que el modelo de la estrategia acepta)."""
        grid = self.strategies[strategy_id].grid
        filters = self.strategies[strategy_id].btc_filter
        model = STRATEGIES[strategy_id].params_model
        names = list(grid)
        combos = []
        for values in itertools.product(*(grid[n] for n in names)):
            params = dict(zip(names, values, strict=True))
            try:
                model.model_validate(params)
            except ValidationError:
                continue  # p. ej. salida más larga que la entrada en rupturas
            if filters == [None]:
                combos.append(params)
            else:
                combos.extend({**params, BTC_FILTER: days} for days in filters)
        return combos


def load_lab_config(config_dir: Path) -> LabConfig:
    return LabConfig.model_validate(read_yaml(config_dir / "lab.yaml"))
