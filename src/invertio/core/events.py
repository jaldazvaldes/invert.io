"""Eventos que circulan por el bus. Son inmutables."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from invertio.core.models import Bar, Fill, Order, OrderRequest, Signal


class EngineState(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"  # no abre posiciones nuevas; sigue gestionando las abiertas
    HALTED = "halted"  # botón de pánico o límite de pérdida: todo parado


class Event:
    __slots__ = ()


@dataclass(frozen=True, slots=True)
class BarClosed(Event):
    """`live=False` para velas de calentamiento o recuperadas con retraso: actualizan
    indicadores y precios, pero sus señales se descartan (serían órdenes a destiempo)."""

    bar: Bar
    live: bool = True


@dataclass(frozen=True, slots=True)
class SignalEmitted(Event):
    signal: Signal


@dataclass(frozen=True, slots=True)
class RiskDecision(Event):
    signal: Signal
    approved: bool
    reason: str
    order: OrderRequest | None = None


@dataclass(frozen=True, slots=True)
class OrderUpdated(Event):
    """`order` es una copia del estado de la orden en el momento del evento."""

    order: Order


@dataclass(frozen=True, slots=True)
class OrderFilled(Event):
    fill: Fill


@dataclass(frozen=True, slots=True)
class EngineStateChanged(Event):
    state: EngineState
    reason: str
