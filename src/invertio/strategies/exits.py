"""Salidas compartidas por varias estrategias."""

from __future__ import annotations

from invertio.core.models import Bar
from invertio.indicators import ATR, RollingMax


class Chandelier:
    """Stop que sigue al precio: máximo de las últimas N velas menos k veces el ATR.

    No depende del momento de la entrada, así que da lo mismo en el laboratorio y en las
    carteras en vivo. Sube con el precio y nunca obliga a vender por un objetivo: deja
    correr las ganancias y corta las pérdidas cuando el precio se da la vuelta.
    """

    def __init__(self, bars: int = 22, atr_period: int = 22) -> None:
        self._highs = RollingMax(bars)
        self._atr = ATR(atr_period)
        self.highest: float | None = None
        self.atr: float | None = None

    def update(self, bar: Bar) -> None:
        self.highest = self._highs.update(bar.high)
        self.atr = self._atr.update(bar.high, bar.low, bar.close)

    def level(self, multiple: float) -> float | None:
        if self.highest is None or self.atr is None or self.atr <= 0:
            return None
        return self.highest - multiple * self.atr
