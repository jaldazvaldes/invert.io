"""Notificaciones: Telegram si está configurado; si no, por consola."""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from typing import Protocol

import structlog

log = structlog.get_logger("invertio.notify")


@dataclass(frozen=True, slots=True)
class Button:
    text: str
    callback_data: str


class Notifier(Protocol):
    async def send(self, text: str, buttons: list[Button] | None = None) -> None:
        """Encola un mensaje (HTML de Telegram). No debe bloquear al que lo llama."""
        ...


class ConsoleNotifier:
    """Sustituto de Telegram: escribe los mensajes en el log."""

    async def send(self, text: str, buttons: list[Button] | None = None) -> None:
        plain = re.sub(r"<[^>]+>", "", text)
        with contextlib.suppress(Exception):  # un aviso fallido nunca debe parar el motor
            log.info("aviso", mensaje=plain)
