"""Contrato de las estrategias.

Una estrategia solo ve velas cerradas y devuelve señales. No envía órdenes ni conoce el
capital: eso lo decide el gestor de riesgo. Así no puede "ver el futuro" y se comporta igual
en backtest, paper y real.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

from invertio.core.models import Bar, Position, Signal


class StrategyContext(Protocol):
    def position(self, venue: str, symbol: str) -> Position: ...


class StrategyParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Strategy[P: StrategyParams](ABC):
    id: ClassVar[str]
    params_model: ClassVar[type[StrategyParams]]

    def __init__(self, params: P) -> None:
        self.params = params

    @property
    @abstractmethod
    def warmup_bars(self) -> int:
        """Velas necesarias antes de poder emitir señales."""

    @abstractmethod
    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[Signal]:
        """Se llama al cierre de cada vela, en orden cronológico."""
