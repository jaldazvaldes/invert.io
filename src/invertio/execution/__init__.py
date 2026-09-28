"""Ejecución: envío de órdenes aprobadas al bróker de cada venue."""

from __future__ import annotations

from typing import Protocol

from invertio.core.bus import EventBus
from invertio.core.events import RiskDecision
from invertio.core.models import Order, OrderRequest


class Broker(Protocol):
    async def submit(self, request: OrderRequest) -> Order: ...

    async def cancel(self, client_order_id: str) -> None: ...

    def open_orders(self) -> list[Order]: ...


class OrderRouter:
    """Envía cada orden aprobada por el gestor de riesgo al bróker de su venue."""

    def __init__(self, bus: EventBus, brokers: dict[str, Broker]) -> None:
        self._brokers = brokers
        bus.subscribe(RiskDecision, self._on_decision)

    async def _on_decision(self, decision: RiskDecision) -> None:
        if decision.approved and decision.order is not None:
            await self._brokers[decision.order.venue].submit(decision.order)
