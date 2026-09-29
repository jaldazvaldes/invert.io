"""Cuentas ficticias con las estrategias aprobadas en el laboratorio; nunca envía órdenes reales."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from invertio.core.models import Bar
from invertio.experiments.candles import LongCandleExperiments
from invertio.experiments.lab_policies import (
    DAILY_SOURCE,
    VERSION,
    daily_from_4h,
    definitions,
    evaluate,
    timeframe,
)
from invertio.experiments.maker import MakerSimulationEngine
from invertio.simulation.config import SimulationConfig
from invertio.simulation.engine import SimulationEngine

KEY = "lab-strategies-v1"
BTC = "BTC/EUR"


class LabExperimentsService(LongCandleExperiments):
    name = "Estrategias aprobadas en el laboratorio"
    identity_prefix = "lab-strategies"
    signal_version = VERSION
    native_timeframes: ClassVar[dict[str, int]] = {"4h": 240, "1h": 60}
    # 1500 velas: 250 días de 4 h (media de 200 días del filtro de BTC) y 62 días de 1 h.
    history_candles: ClassVar[int] = 1500

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._btc_daily: list[Bar] = []
        self._signals: dict[tuple[str, str], tuple[Any, dict[str, Any]]] = {}

    def _catalog(self) -> list[dict[str, Any]]:
        return definitions()

    def _engine(
        self, definition: dict[str, Any], state: dict[str, Any] | None, now: datetime
    ) -> SimulationEngine:
        return MakerSimulationEngine(
            SimulationConfig.model_validate(definition["simulation_config"]),
            state,
            now,
            pending_minutes=definition["pending_minutes"],
        )

    def before_decisions(self, symbols: list[str], now: datetime) -> None:
        self._btc_daily = daily_from_4h(self.native(BTC, DAILY_SOURCE))

    def _policy(self, ident: str, bars: list[Bar], now: datetime) -> dict[str, Any]:
        # ``bars`` son las velas de un minuto del análisis: dan el mercado y la frescura.
        native = self.native(bars[-1].symbol, timeframe(ident)) if bars else []
        return evaluate(ident, native, bars, self._btc_daily, now, cache=self._signals)
