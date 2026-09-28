import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from invertio.core.bus import EventBus
from invertio.core.clock import SimClock
from invertio.core.events import EngineState, OrderFilled
from invertio.core.models import Fill, Side, TradingMode
from invertio.execution.sim import SimBroker
from invertio.live.controller import EngineController
from invertio.live.feeds import SpreadBook
from invertio.notify import Button
from invertio.notify.format import money, number, pct, price, quantity, signed_money
from invertio.notify.telegram import TelegramClient, TelegramCommands, TelegramError
from invertio.portfolio import Portfolio
from invertio.risk import RiskManager
from tests.helpers import make_bars, repo_config

TOKEN = "123456:SECRETO"
CHAT = 42
API = f"https://api.telegram.org/bot{TOKEN}"
D = Decimal


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, list[Button] | None]] = []

    async def send(self, text: str, buttons: list[Button] | None = None) -> None:
        self.sent.append((text, buttons))


async def _controller(t0: datetime) -> tuple[EngineController, Portfolio, EventBus]:
    config = repo_config()
    bus = EventBus(strict=True)
    clock = SimClock(t0)
    portfolio = Portfolio(bus, {"revolutx": D(100)}, {"revolutx": "EUR"})
    risk = RiskManager(bus, clock, config, portfolio)
    broker = SimBroker(bus, clock, config.venue("revolutx"), portfolio)
    controller = EngineController(
        bus, clock, config, TradingMode.PAPER, portfolio, risk, {"revolutx": broker}
    )
    fill = Fill("x", "revolutx", "BTC/EUR", Side.BUY, D("0.001"), D("50000"), D(0), "EUR", t0)
    await bus.publish(OrderFilled(fill))
    from invertio.core.events import BarClosed

    await bus.publish(BarClosed(make_bars([51000], t0)[0]))
    return controller, portfolio, bus


def _commands(controller: EngineController) -> tuple[TelegramCommands, RecordingNotifier]:
    notifier = RecordingNotifier()
    client = TelegramClient(TOKEN, httpx.AsyncClient())
    return TelegramCommands(client, CHAT, notifier, controller), notifier  # type: ignore[arg-type]


def _message(text: str, chat: int = CHAT) -> dict[str, Any]:
    return {"update_id": 1, "message": {"chat": {"id": chat}, "text": text}}


async def test_ignores_other_chats(t0: datetime) -> None:
    controller, _, _ = await _controller(t0)
    commands, notifier = _commands(controller)
    await commands.handle(_message("/panico", chat=999))
    await commands.handle(_message("/status", chat=999))
    assert notifier.sent == []


async def test_status_and_positions(t0: datetime) -> None:
    controller, _, _ = await _controller(t0)
    commands, notifier = _commands(controller)
    await commands.handle(_message("/status"))
    text = notifier.sent[-1][0]
    assert "en marcha" in text and "revolutx" in text and "BTC/EUR" in text
    await commands.handle(_message("/posiciones@invertio_bot"))
    text = notifier.sent[-1][0]
    assert "entrada 50.000,00" in text and "ahora 51.000,00" in text and "+1,00 €" in text


async def test_pause_and_resume(t0: datetime) -> None:
    controller, _, _ = await _controller(t0)
    commands, _ = _commands(controller)
    await commands.handle(_message("/pausa"))
    after_pause = controller.state
    await commands.handle(_message("/reanudar"))
    assert after_pause is EngineState.PAUSED
    assert controller.state is EngineState.RUNNING


@respx.mock
async def test_panic_requires_confirmation(t0: datetime) -> None:
    respx.post(f"{API}/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )
    controller, portfolio, _ = await _controller(t0)
    commands, notifier = _commands(controller)

    await commands.handle(_message("/panico"))
    text, buttons = notifier.sent[-1]
    assert "¿Cerrar" in text and buttons is not None
    assert portfolio.open_positions()  # aún no ha pasado nada

    callback = {
        "update_id": 2,
        "callback_query": {"id": "cb1", "data": "panic:confirm", "message": {"chat": {"id": CHAT}}},
    }
    await commands.handle(callback)
    assert not portfolio.open_positions()
    assert controller.state is EngineState.HALTED
    assert "1 posiciones cerradas" in notifier.sent[-1][0]


@respx.mock
async def test_send_message_payload_and_errors_never_show_the_token() -> None:
    route = respx.post(f"{API}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )
    client = TelegramClient(TOKEN, httpx.AsyncClient())
    await client.send_message(CHAT, "<b>hola</b>", [Button("Sí", "panic:confirm")])
    payload = json.loads(route.calls[0].request.content)
    assert payload["parse_mode"] == "HTML"
    assert payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "panic:confirm"

    respx.post(f"{API}/getUpdates").mock(side_effect=httpx.ConnectError("fallo"))
    with pytest.raises(TelegramError) as exc_info:
        await client.get_updates(None)
    assert TOKEN not in str(exc_info.value) and "SECRETO" not in str(exc_info.value)

    respx.post(f"{API}/sendMessage").mock(
        return_value=httpx.Response(400, json={"ok": False, "description": "chat not found"})
    )
    with pytest.raises(TelegramError, match="chat not found"):
        await client.send_message(CHAT, "x")


def test_spanish_number_format() -> None:
    assert number(D("73200.5")) == "73.200,50"
    assert price(D("0.00012345")) == "0,00012345"
    assert price(2355.33) == "2.355,33"
    assert money(D("49.8"), "EUR") == "49,80 €"
    assert signed_money(D("-0.52"), "USD") == "−0,52 $"
    assert pct(1.5) == "+1,50 %" and pct(-0.96) == "−0,96 %"
    assert quantity(D("0.00068000")) == "0,00068"


def test_spread_book(t0: datetime) -> None:
    clock = SimClock(t0)
    book = SpreadBook(clock, {"revolutx": None, "trading212": 0.05}, max_age=timedelta(minutes=10))
    assert book.get("revolutx", "BTC/EUR") is None  # sin dato aún: no se entra a ciegas
    book.update("revolutx", "BTC/EUR", 0.012)
    assert book.get("revolutx", "BTC/EUR") == 0.012
    assert book.get("trading212", "AAPL") == 0.05  # sin spread en vivo: supuesto de config
    clock.set(t0 + timedelta(minutes=11))
    assert book.get("revolutx", "BTC/EUR") is None  # dato viejo
