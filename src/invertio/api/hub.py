"""Retransmite los eventos del motor a los paneles conectados por WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from invertio.core.bus import EventBus
from invertio.core.events import (
    BarClosed,
    EngineStateChanged,
    OrderFilled,
    OrderUpdated,
    RiskDecision,
)

CLOSE: dict[str, Any] = {"type": "close"}
MAX_QUEUE = 500  # un panel lento no debe frenar al motor: si se llena, se descartan avisos


def _market(venue: str, symbol: str) -> str:
    return f"{venue}:{symbol}"


class EventHub:
    def __init__(self) -> None:
        self._clients: set[asyncio.Queue[dict[str, Any]]] = set()

    def attach(self, bus: EventBus) -> None:
        bus.subscribe(BarClosed, self._on_bar)
        bus.subscribe(RiskDecision, self._on_decision)
        bus.subscribe(OrderUpdated, self._on_order)
        bus.subscribe(OrderFilled, self._on_fill)
        bus.subscribe(EngineStateChanged, self._on_state)

    def connect(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=MAX_QUEUE)
        self._clients.add(queue)
        return queue

    def disconnect(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._clients.discard(queue)

    @property
    def clients(self) -> int:
        return len(self._clients)

    def close_all(self) -> None:
        """Pide a todos los paneles conectados que cierren (al apagar el servidor)."""
        for queue in list(self._clients):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(CLOSE)

    def broadcast(self, message: dict[str, Any]) -> None:
        for queue in list(self._clients):
            if queue.qsize() < MAX_QUEUE - 1:  # se reserva un hueco para el cierre
                queue.put_nowait(message)

    def _on_bar(self, event: BarClosed) -> None:
        if event.live:
            bar = event.bar
            self.broadcast(
                {
                    "type": "bar",
                    "market": _market(bar.venue, bar.symbol),
                    "close_time": bar.close_time.isoformat(),
                    "close": bar.close,
                }
            )

    def _on_decision(self, event: RiskDecision) -> None:
        s = event.signal
        self.broadcast(
            {
                "type": "signal",
                "market": _market(s.venue, s.symbol),
                "action": s.action.value,
                "approved": event.approved,
                "reason": event.reason,
            }
        )

    def _on_order(self, event: OrderUpdated) -> None:
        r = event.order.request
        self.broadcast(
            {
                "type": "order",
                "market": _market(r.venue, r.symbol),
                "side": r.side.value,
                "status": event.order.status.value,
            }
        )

    def _on_fill(self, event: OrderFilled) -> None:
        f = event.fill
        self.broadcast(
            {
                "type": "fill",
                "market": _market(f.venue, f.symbol),
                "side": f.side.value,
                "price": float(f.price),
                "quantity": float(f.quantity),
            }
        )

    def _on_state(self, event: EngineStateChanged) -> None:
        self.broadcast({"type": "state", "state": event.state.value, "reason": event.reason})
