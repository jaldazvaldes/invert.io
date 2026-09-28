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


class StrategyGrid(_Strict):
    timeframes: list[str] = Field(min_length=1)
    grid: dict[str, list[Any]] = Field(default_factory=dict)

    @field_validator("timeframes")
    @classmethod
    def _valid_timeframes(cls, value: list[str]) -> list[str]:
        for tf in value:
            timeframe_seconds(tf)
        return value


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
        model = STRATEGIES[strategy_id].params_model
        names = list(grid)
        combos = []
        for values in itertools.product(*(grid[n] for n in names)):
            params = dict(zip(names, values, strict=True))
            try:
                model.model_validate(params)
            except ValidationError:
                continue  # p. ej. salida más larga que la entrada en rupturas
            combos.append(params)
        return combos


def load_lab_config(config_dir: Path) -> LabConfig:
    return LabConfig.model_validate(read_yaml(config_dir / "lab.yaml"))
