"""Grupo de estrategias con velas de 10 minutos y de una hora; nunca envía órdenes reales.

Las velas largas las gestiona `LongCandleExperiments` (experiments/candles.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from invertio.core.models import Bar
from invertio.experiments.candles import HISTORY_CANDLES, LongCandleExperiments
from invertio.experiments.timeframe_policies import (
    NATIVE_TIMEFRAMES,
    VERSION,
    definitions,
    evaluate,
    native_timeframe,
)

__all__ = ["HISTORY_CANDLES", "KEY", "TimeframeExperimentsService"]

KEY = "timeframes-v1"


class TimeframeExperimentsService(LongCandleExperiments):
    name = "Velas de 10 minutos y 1 hora"
    identity_prefix = "timeframes"
    signal_version = VERSION
    native_timeframes: ClassVar[dict[str, int]] = NATIVE_TIMEFRAMES

    def _catalog(self) -> list[dict[str, Any]]:
        return definitions()

    def _policy(self, ident: str, bars: list[Bar], now: datetime) -> dict[str, Any]:
        # ``bars`` son las velas de un minuto del análisis: dan el mercado y la frescura.
        native = self.native(bars[-1].symbol, native_timeframe(ident)) if bars else []
        return evaluate(ident, native, bars, now)
