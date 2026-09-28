"""Durable analysis alerts, delivered only after acknowledgement by the sender.

The sender must return only after Telegram acknowledges sendMessage and must raise on
failure (TelegramClient.send_message fulfils this contract). Queueing a message is not
an acknowledgement. Persistence prevents normal duplicates across cycles/restarts.
Telegram provides no idempotency key: a crash or lost response after remote acceptance
but before the local commit can cause a duplicate on retry. This is not exactly-once.
One local analysis service owns the outbox; parallel processes are not supported.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any

from invertio.analysis.repository import AnalysisRepository


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _number(value: Any, decimals: int = 8) -> str:
    if value is None:
        return "sin datos"
    try:
        return f"{float(value):.{decimals}f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(value)[:80]


def _reasons(values: Any) -> str:
    if isinstance(values, list):
        return "; ".join(str(value)[:250] for value in values[:8]) or "ninguno"
    return str(values)[:800] if values else "ninguno"


def _levels(opportunity: dict[str, Any]) -> str:
    return (
        f"Entrada estimada: {_number(opportunity.get('entry'))} EUR\n"
        f"Stop: {_number(opportunity.get('stop'))} EUR · "
        f"Objetivo: {_number(opportunity.get('target'))} EUR\n"
        f"Referencia: {_number(opportunity.get('reference_eur', 100), 2)} EUR"
    )


def format_start(opportunity: dict[str, Any]) -> str:
    """Format the frozen signal snapshot, escaping all caller-controlled HTML."""
    points = opportunity.get("points", opportunity.get("factors", {}))
    maxima = opportunity.get("max_points", {})
    if isinstance(points, dict):
        factors = "; ".join(
            f"{name}: {_number(value, 2)}"
            + (f"/{_number(maxima[name], 2)}" if name in maxima else "")
            for name, value in points.items()
        )
    else:
        factors = _reasons(points)
    costs = opportunity.get("costs", {})
    cost_text = (
        f"Ida y vuelta: {_number(costs.get('round_trip_cost_eur'), 4)} EUR "
        f"({_number(costs.get('round_trip_cost_pct'), 3)} %). "
        f"Spread: {_number(costs.get('spread_pct'), 3)} %. "
        f"Deslizamiento compra/venta: {_number(costs.get('buy_slippage_pct'), 3)} % / "
        f"{_number(costs.get('sell_slippage_pct'), 3)} %. "
        f"Objetivo neto estimado: {_number(costs.get('target_net_pct'), 3)} %."
        if isinstance(costs, dict)
        else "sin datos"
    )
    text = (
        f"invert.io · Oportunidad experimental · {opportunity.get('market', '')}\n"
        f"Puntuación de reglas: {_number(opportunity.get('score'), 2)}/100\n"
        f"{_levels(opportunity)}\n"
        f"Factores: {factors}\n"
        f"A favor: {_reasons(opportunity.get('positive', opportunity.get('reasons_for')))}\n"
        f"En contra: {_reasons(opportunity.get('negative', opportunity.get('reasons_against')))}\n"
        f"Costes estimados: {cost_text}\n"
        f"Observada: {opportunity.get('created_at', '')}\n"
        "Seguimiento hipotético; no representa una compra ejecutada. "
        "La puntuación no es un porcentaje de acierto."
    )
    return escape(text[:3500], quote=False)


def format_end(opportunity: dict[str, Any]) -> str:
    outcomes = {
        "target": "objetivo observado",
        "stop": "stop observado",
        "score": "puntuación de salida",
        "filters": "ya no cumple filtros",
        "expired": "caducada",
        "ambiguous": "orden desconocido",
        "interrupted": "seguimiento interrumpido",
    }
    qualities = {"complete": "completa", "incomplete": "incompleta", "ambiguous": "ambigua"}
    quality = opportunity.get("quality", "incomplete")
    result = opportunity.get("estimated_return_pct")
    result_text = (
        f"Retorno neto estimado: {_number(result, 3)} %\n"
        if quality == "complete" and result is not None
        else "Sin retorno atribuible\n"
    )
    text = (
        f"invert.io · Fin de oportunidad experimental · {opportunity.get('market', '')}\n"
        f"Desenlace: {outcomes.get(opportunity.get('outcome', ''), 'sin resultado')}\n"
        f"Motivo: {opportunity.get('reason') or 'Sin detalle'}\n"
        f"Calidad de observación: {qualities.get(quality, 'sin datos')}\n"
        f"{result_text}"
        f"{_levels(opportunity)}\n"
        f"Fin: {opportunity.get('ended_at') or opportunity.get('closed_at') or ''}\n"
        "Resultado hipotético del seguimiento; no representa una operación ejecutada."
    )
    return escape(text[:3500], quote=False)


class NotificationDispatcher:
    def __init__(
        self,
        repo: AnalysisRepository,
        sender: Callable[[str], Awaitable[None]] | None = None,
        clock: Callable[[], datetime] = _utcnow,
        *,
        send_interval: float = 1.05,
    ) -> None:
        self._repo = repo
        self._sender = sender
        self._clock = clock
        self._send_interval = max(0.0, send_interval)
        self._last_attempt: float | None = None
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return self._sender is not None

    async def flush(self) -> None:
        """Retry durable pending/failed entries. Delivery errors never stop analysis."""
        async with self._lock:
            for notification in await self._repo.pending_notifications(self._clock()):
                if await self._expire_or_block(notification):
                    continue
                if self._sender is None:
                    continue
                loop = asyncio.get_running_loop()
                if self._last_attempt is not None:
                    delay = self._last_attempt + self._send_interval - loop.time()
                    if delay > 0:
                        await asyncio.sleep(delay)
                # A waiting message may have become stale during the rate-limit pause.
                if await self._expire_or_block(notification):
                    continue
                self._last_attempt = loop.time()
                try:
                    await self._sender(notification["text"])
                except Exception as exc:
                    # Arbitrary HTTP exceptions may contain a bot token in their URL.
                    # Persist the class only; never persist untrusted exception text.
                    await self._repo.mark_notification(
                        notification["id"],
                        "failed",
                        error=f"Delivery failed ({type(exc).__name__})",
                    )
                else:
                    await self._repo.mark_notification(
                        notification["id"],
                        "sent",
                        now=self._clock(),
                    )

    async def _expire_or_block(self, notification: dict[str, Any]) -> bool:
        if notification["kind"] == "start":
            expiry = (
                datetime.fromisoformat(notification["expires_at"])
                if notification.get("expires_at")
                else (datetime.fromisoformat(notification["created_at"]) + timedelta(minutes=1))
            )
            if self._clock() >= expiry:
                await self._repo.mark_notification(
                    notification["id"],
                    "expired",
                    error="Start no longer current",
                )
                return True
            return False
        entries = await self._repo.notifications(notification["opportunity_id"])
        start = next((entry for entry in entries if entry["kind"] == "start"), None)
        if start is None or start["status"] == "expired":
            await self._repo.mark_notification(
                notification["id"],
                "expired",
                error="Start was not delivered",
            )
            return True
        return bool(start["status"] != "sent")
