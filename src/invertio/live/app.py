"""Monta el motor en vivo a partir de la configuración y lo ejecuta hasta Ctrl+C."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

import structlog

from invertio.api.app import create_app
from invertio.api.context import ApiContext
from invertio.api.hub import EventHub
from invertio.api.server import build_server, port_available
from invertio.config.app_config import AppConfig
from invertio.config.live_config import LiveConfig
from invertio.config.settings import Settings
from invertio.core.clock import LiveClock
from invertio.core.models import AssetClass
from invertio.data.store import BarStore
from invertio.live.engine import LiveEngine, MarketRuntime
from invertio.live.feeds import AlpacaLiveFeed, CcxtLiveFeed, LiveFeed
from invertio.notify import ConsoleNotifier, Notifier
from invertio.notify.telegram import TelegramClient, TelegramCommands, TelegramNotifier
from invertio.persistence.db import create_engine_async, session_factory, upgrade_db
from invertio.strategies import load_strategy

log = structlog.get_logger(__name__)


def build_markets(
    settings: Settings,
    config: AppConfig,
    live: LiveConfig,
    warn: Callable[[str], None],
) -> list[MarketRuntime]:
    """Un feed por venue, compartido por sus mercados. Omite (con aviso) lo que no se pueda usar."""
    feeds: dict[str, LiveFeed] = {}
    markets = []
    for entry in live.markets:
        venue = config.venue(entry.venue)
        if not venue.enabled:
            warn(f"{entry.market}: venue desactivado en app.yaml, se omite")
            continue
        if venue.id not in feeds:
            if venue.asset_class is AssetClass.STOCK:
                if settings.alpaca_api_key is None or settings.alpaca_api_secret is None:
                    warn(f"{entry.market}: faltan ALPACA_API_KEY/SECRET para datos de acciones")
                    continue
                feeds[venue.id] = AlpacaLiveFeed(
                    venue.id,
                    settings.alpaca_api_key.get_secret_value(),
                    settings.alpaca_api_secret.get_secret_value(),
                )
            else:
                # Paper usa los datos públicos del propio venue donde se operará.
                feeds[venue.id] = CcxtLiveFeed(venue.id, venue.id)
        markets.append(
            MarketRuntime(
                venue=venue,
                symbol=entry.symbol,
                strategy=load_strategy(entry.strategy, settings.config_dir),
                feed=feeds[venue.id],
            )
        )
    return markets


async def run_live(
    settings: Settings,
    config: AppConfig,
    live: LiveConfig,
    timeframe: str,
    warn: Callable[[str], None],
    *,
    panel: bool = True,
) -> None:
    upgrade_db(settings)
    markets = build_markets(settings, config, live, warn)
    if not markets:
        raise ValueError("Ningún mercado de live.yaml se puede usar")

    telegram: TelegramClient | None = None
    notifier: Notifier
    if settings.telegram_bot_token is not None and settings.telegram_chat_id is not None:
        telegram = TelegramClient(settings.telegram_bot_token.get_secret_value())
        notifier = TelegramNotifier(telegram, settings.telegram_chat_id)
    else:
        warn("Telegram no configurado: los avisos se mostrarán aquí")
        notifier = ConsoleNotifier()

    db = create_engine_async(settings)
    sessions = session_factory(db)
    store = BarStore(settings.data_dir / "bars")
    engine = LiveEngine(
        config=config,
        live=live,
        mode=settings.trading_mode,
        timeframe=timeframe,
        markets=markets,
        sessions=sessions,
        notifier=notifier,
        clock=LiveClock(),
        store=store,
    )

    stop = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    server = None
    hub: EventHub | None = None
    if panel:
        if port_available(settings.panel_port):
            hub = EventHub()
            hub.attach(engine.bus)
            context = ApiContext(settings, config, live, sessions, store, engine=engine)
            server = build_server(create_app(context, hub), settings.panel_port)
            tasks.append(asyncio.create_task(server.serve(), name="panel"))
        else:
            warn(f"El puerto {settings.panel_port} está ocupado: el panel no se arranca")
    if isinstance(notifier, TelegramNotifier):
        notifier.start()
    try:
        if telegram is not None and settings.telegram_chat_id is not None:
            assert isinstance(notifier, TelegramNotifier)
            commands = TelegramCommands(
                telegram, settings.telegram_chat_id, notifier, engine.controller
            )
            try:
                await commands.register_commands()
            except Exception as exc:
                warn(f"No se pudieron registrar los comandos de Telegram: {exc}")
            tasks.append(asyncio.create_task(commands.run(), name="telegram-commands"))
        await engine.run(stop)
    finally:
        stop.set()
        if hub is not None:
            hub.close_all()
        if server is not None:
            server.should_exit = True
        for task in tasks:
            if task.get_name() == "panel":
                with contextlib.suppress(Exception):
                    await task
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if isinstance(notifier, TelegramNotifier):
            await notifier.stop()
        if telegram is not None:
            await telegram.close()
        await db.dispose()
