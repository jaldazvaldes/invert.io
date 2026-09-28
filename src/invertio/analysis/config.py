"""Configuración independiente del análisis público de Revolut X."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from invertio.config.app_config import read_yaml
from invertio.strategies.score import ScoreParams, ScoreStrategy


class AnalysisConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False, strict=True)

    venue: Literal["revolutx"] = "revolutx"
    timeframe: Literal["1m"] = "1m"
    quote: Literal["EUR"] = "EUR"
    region: Literal["EEA"] = "EEA"
    interval_seconds: int = Field(default=60, ge=60)
    selection_seconds: int = Field(default=300, ge=60)
    market_scope: Literal["top", "all_eur"] = "top"
    max_markets: int = Field(default=20, ge=1, le=20)
    max_spread_pct: float = Field(default=0.3, ge=0, lt=100)
    max_slippage_pct: float = Field(default=0.1, ge=0, lt=100)
    reference_eur: float = Field(default=100.0, gt=0)
    taker_fee_pct: float = Field(default=0.09, ge=0, lt=100)
    quote_max_age_seconds: int = Field(default=60, ge=1)
    bar_max_age_seconds: int = Field(default=120, ge=1)
    lifetime_minutes: int = Field(default=240, ge=1)
    entry_score: float = Field(default=70.0, gt=0, le=100)
    exit_score: float = Field(default=40.0, ge=0, lt=100)
    stop_atr: float = Field(default=2.0, gt=0)
    target_atr: float = Field(default=3.0, gt=0)
    score_params: ScoreParams = Field(default_factory=lambda: ScoreParams(min_atr_pct=0))

    @model_validator(mode="after")
    def _consistent(self) -> AnalysisConfig:
        if self.selection_seconds < self.interval_seconds:
            raise ValueError("selection_seconds debe ser >= interval_seconds")
        if self.lifetime_minutes * 60 < self.interval_seconds:
            raise ValueError("La duración debe permitir al menos un ciclo de seguimiento")
        if self.exit_score >= self.entry_score:
            raise ValueError("exit_score debe ser menor que entry_score")
        if self.score_params.min_atr_pct != 0:
            raise ValueError(
                "El análisis calcula costes reales; score_params.min_atr_pct debe ser 0"
            )

        def finite(value: Any) -> bool:
            if isinstance(value, dict):
                return all(finite(item) for item in value.values())
            return not isinstance(value, float) or math.isfinite(value)

        if not finite(self.score_params.model_dump()):
            raise ValueError("Los parámetros de puntuación deben ser finitos")
        return self

    @property
    def warmup_bars(self) -> int:
        return max(
            ScoreStrategy(self.score_params).warmup_bars,
            self.score_params.atr_period + 1,
            self.score_params.rsi_period + 1,
        )


def load_analysis_config(config_dir: Path) -> AnalysisConfig:
    path = config_dir / "analysis.yaml"
    return AnalysisConfig.model_validate(read_yaml(path)) if path.exists() else AnalysisConfig()
