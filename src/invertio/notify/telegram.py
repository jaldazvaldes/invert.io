"""Bot de Telegram: avisos y comandos (/status, /posiciones, /hoy, /pausa, /reanudar, /panico).

Seguridad: el bot solo atiende a TELEGRAM_CHAT_ID; ignora cualquier otro chat. El token no se
escribe nunca en los logs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import structlog

from invertio.core.events import EngineState
from invertio.live.controller import EngineController
from invertio.notify import Button
from invertio.notify.format import html, money, pct, price, quantity, signed_money
from invertio.strategies.score import points_str

log = structlog.get_logger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
SEND_INTERVAL = 1.05  # Telegram limita a ~1 mensaje por segundo en un mismo chat

COMMANDS = [
    ("status", "Capital, posiciones y estado del motor"),
    ("posiciones", "Posiciones abiertas con stop y objetivo"),
    ("hoy", "Resumen del día"),
    ("puntos", "Nota actual (0-100) de cada mercado vigilado"),
    ("pausa", "No abrir posiciones nuevas"),
    ("reanudar", "Volver a operar"),
    ("panico", "Cerrar todo y detener el motor"),
    ("ayuda", "Lista de comandos"),
]


class TelegramError(Exception):
    pass


class TelegramClient:
    def __init__(self, token: str, http: httpx.AsyncClient | None = None) -> None:
        self._token = token
        self._http = http or httpx.AsyncClient()

    async def call(self, method: str, *, http_timeout: float = 15, **payload: Any) -> Any:
        try:
            response = await self._http.post(
                API.format(token=self._token, method=method), json=payload, timeout=http_timeout
            )
        except httpx.HTTPError as exc:
            # El mensaje de httpx incluye la URL, que contiene el token: no se propaga.
            raise TelegramError(f"{method}: error de red ({type(exc).__name__})") from None
        data = response.json()
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description', response.status_code)}")
        return data["result"]

    async def send_message(
        self, chat_id: int, text: str, buttons: list[Button] | None = None
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": b.text, "callback_data": b.callback_data} for b in buttons]
                ]
            }
        await self.call("sendMessage", **payload)

    async def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict[str, Any]]:
        result = await self.call(
            "getUpdates",
            http_timeout=timeout + 10,
            offset=offset,
            timeout=timeout,  # long polling: Telegram espera hasta `timeout` s a que haya algo
            allowed_updates=["message", "callback_query"],
        )
        return list(result)

    async def close(self) -> None:
        await self._http.aclose()


class TelegramNotifier:
    """Cola de mensajes con envío en segundo plano: quien avisa nunca espera a la red."""

    def __init__(self, client: TelegramClient, chat_id: int) -> None:
        self._client = client
        self._chat_id = chat_id
        self._queue: asyncio.Queue[tuple[str, list[Button] | None] | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def send(self, text: str, buttons: list[Button] | None = None) -> None:
        self._queue.put_nowait((text, buttons))

    def start(self) -> None:
        self._task = asyncio.create_task(self._sender(), name="telegram-sender")

    async def stop(self) -> None:
        if self._task is not None:
            await self._queue.put(None)
            await self._task
            self._task = None

    async def _sender(self) -> None:
        while (item := await self._queue.get()) is not None:
            text, buttons = item
            for attempt in range(3):
                try:
                    await self._client.send_message(self._chat_id, text, buttons)
                    break
                except TelegramError as exc:
                    log.warning("no se pudo enviar a Telegram", error=str(exc), intento=attempt + 1)
                    await asyncio.sleep(2 * (attempt + 1))
            await asyncio.sleep(SEND_INTERVAL)


type Handler = Callable[[], Awaitable[None]]


class TelegramCommands:
    """Atiende los comandos del chat autorizado mediante long polling."""

    def __init__(
        self,
        client: TelegramClient,
        chat_id: int,
        notifier: TelegramNotifier,
        controller: EngineController,
    ) -> None:
        self._client = client
        self._chat_id = chat_id
        self._notifier = notifier
        self._controller = controller
        self._offset: int | None = None
        self._handlers: dict[str, Handler] = {
            "start": self._help,
            "ayuda": self._help,
            "help": self._help,
            "status": self._status,
            "posiciones": self._positions,
            "hoy": self._today,
            "puntos": self._scores,
            "pausa": self._pause,
            "reanudar": self._resume,
            "panico": self._panic_ask,
        }

    async def register_commands(self) -> None:
        await self._client.call(
            "setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS]
        )

    async def run(self) -> None:
        while True:
            try:
                updates = await self._client.get_updates(self._offset)
            except TelegramError as exc:
                log.warning("error leyendo comandos de Telegram", error=str(exc))
                await asyncio.sleep(5)
                continue
            for update in updates:
                self._offset = update["update_id"] + 1
                try:
                    await self.handle(update)
                except Exception:
                    log.exception("error atendiendo un comando de Telegram")

    async def handle(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            query = update["callback_query"]
            chat = query.get("message", {}).get("chat", {}).get("id")
            if chat != self._chat_id:
                log.warning("callback de un chat no autorizado ignorado", chat=chat)
                return
            await self._client.call("answerCallbackQuery", callback_query_id=query["id"])
            if query.get("data") == "panic:confirm":
                await self._panic()
            elif query.get("data") == "panic:cancel":
                await self._notifier.send("Pánico cancelado. Todo sigue igual.")
            return
        message = update.get("message") or {}
        chat = message.get("chat", {}).get("id")
        if chat != self._chat_id:
            log.warning("mensaje de un chat no autorizado ignorado", chat=chat)
            return
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return
        command = text[1:].split()[0].split("@")[0].lower()
        handler = self._handlers.get(command)
        if handler is None:
            await self._notifier.send("Comando desconocido. Usa /ayuda.")
            return
        await handler()

    # --- comandos ----------------------------------------------------------------------

    async def _help(self) -> None:
        lines = ["<b>invert.io</b> · comandos:"] + [f"/{c} — {html(d)}" for c, d in COMMANDS]
        await self._notifier.send("\n".join(lines))

    async def _status(self) -> None:
        c = self._controller
        state = {
            EngineState.RUNNING: "▶️ en marcha",
            EngineState.PAUSED: "⏸ en pausa",
            EngineState.HALTED: "🛑 detenido",
        }[c.state]
        lines = [f"<b>Estado</b>: {state} · modo <b>{c.mode.value}</b>"]
        for venue in c.status():
            change = float(venue.equity / venue.initial - 1) * 100 if venue.initial else 0.0
            lines.append(
                f"\n<b>{html(venue.venue)}</b>: {money(venue.equity, venue.currency)} "
                f"({pct(change)} desde el inicio) · efectivo {money(venue.cash, venue.currency)}"
            )
            for p in venue.positions:
                lines.append(
                    f"• {html(p.symbol)} {quantity(p.quantity)} · "
                    f"{signed_money(p.unrealized, venue.currency)}"
                )
        if c.last_bar:
            newest = max(c.last_bar.values())
            lines.append(f"\nÚltima vela: {newest:%H:%M} UTC")
        await self._notifier.send("\n".join(lines))

    async def _positions(self) -> None:
        lines = []
        for venue in self._controller.status():
            for p in venue.positions:
                last = price(p.last_price) if p.last_price else "—"
                target = price(p.take_profit) if p.take_profit else "por señal"
                stop = price(p.stop_loss) if p.stop_loss else "—"
                lines.append(
                    f"<b>{html(p.symbol)}</b> ({html(venue.venue)}): {quantity(p.quantity)}\n"
                    f"entrada {price(p.avg_price)} · ahora {last} · "
                    f"{signed_money(p.unrealized, venue.currency)}\n"
                    f"stop {stop} · objetivo {target}"
                )
        await self._notifier.send("\n\n".join(lines) or "No hay posiciones abiertas.")

    async def _today(self) -> None:
        s = self._controller.today()
        pnl = ", ".join(signed_money(v, c) for c, v in s.pnl_by_currency.items()) or "—"
        lines = [
            "<b>Hoy</b>",
            f"Operaciones cerradas: {s.trades} ({s.wins} ganadoras) · resultado {pnl}",
            f"Señales: {s.signals} · aprobadas {s.approved}",
        ]
        if s.rejections:
            lines.append("Rechazos: " + "; ".join(f"{html(r)} ({n})" for r, n in s.rejections))
        await self._notifier.send("\n".join(lines))

    async def _scores(self) -> None:
        scores = self._controller.scores()
        if not scores:
            await self._notifier.send(
                "Ningún mercado vigilado usa la estrategia de puntuación (live.yaml)."
            )
            return
        meeting = sum(1 for _, meets in scores if meets)
        lines = [
            f"<b>Puntuación actual</b> (0-100) · {len(scores)} mercados · {meeting} cumplen",
            "Los 10 mejores:",
        ]
        for score, meets in scores[:10]:
            flag = " ✅ cumple" if meets else "" if score.tradable else " · no cubre costes"
            lines.append(
                f"<b>{html(score.symbol)}</b>: {points_str(score.score)}{flag}\n"
                f"<i>{html(score.breakdown())}</i>"
            )
        await self._notifier.send("\n".join(lines))

    async def _pause(self) -> None:
        await self._controller.pause("pedido por Telegram")

    async def _resume(self) -> None:
        await self._controller.resume("pedido por Telegram")

    async def _panic_ask(self) -> None:
        await self._notifier.send(
            "⚠️ ¿Cerrar <b>todas</b> las posiciones, cancelar las órdenes y detener el motor?",
            buttons=[
                Button("Sí, cerrar todo", "panic:confirm"),
                Button("Cancelar", "panic:cancel"),
            ],
        )

    async def _panic(self) -> None:
        closed = await self._controller.panic("botón de pánico (Telegram)")
        await self._notifier.send(
            f"🛑 Pánico ejecutado: {closed} posiciones cerradas. Usa /reanudar para volver."
        )
