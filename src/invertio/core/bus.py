"""Bus de eventos en proceso.

El reparto es secuencial y determinista: `publish` espera a cada handler, en orden de
suscripción, antes de volver. Así una vela desencadena toda la cadena
señal → riesgo → orden → fill dentro de la misma llamada, y un backtest se reproduce
exactamente igual cada vez.

Los handlers lentos (por ejemplo, enviar un mensaje de Telegram) deben lanzar su propia
tarea en segundo plano para no frenar la cadena.
"""

from __future__ import annotations

import inspect
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from invertio.core.events import Event

log = structlog.get_logger(__name__)

type Handler[E: Event] = Callable[[E], Awaitable[None] | None]


class EventBus:
    def __init__(self, *, strict: bool = False) -> None:
        """Con `strict=True` (backtests y tests) un error en un handler se propaga.
        Con `strict=False` (en vivo) se registra y el resto de handlers sigue recibiendo el evento.
        """
        self._strict = strict
        self._handlers: defaultdict[type[Event], list[Handler[Any]]] = defaultdict(list)

    def subscribe[E: Event](self, event_type: type[E], handler: Handler[E]) -> Callable[[], None]:
        """Suscribe `handler` a `event_type` y sus subclases. Devuelve la función para anular."""
        self._handlers[event_type].append(handler)

        def unsubscribe() -> None:
            handlers = self._handlers[event_type]
            if handler in handlers:
                handlers.remove(handler)

        return unsubscribe

    async def publish(self, event: Event) -> None:
        for event_type in type(event).__mro__:
            for handler in list(self._handlers.get(event_type, ())):
                try:
                    result = handler(event)
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    if self._strict:
                        raise
                    log.exception(
                        "error en handler de evento",
                        event_type=type(event).__name__,
                        handler=getattr(handler, "__qualname__", repr(handler)),
                    )
