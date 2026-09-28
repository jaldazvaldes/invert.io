"""Arranque y cierre explícitos del análisis local y su panel."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

from invertio.analysis.config import AnalysisConfig
from invertio.analysis.market import AnalysisFeed, PublicAnalysisFeed
from invertio.analysis.notifications import NotificationDispatcher
from invertio.analysis.repository import AnalysisRepository
from invertio.analysis.service import AnalysisService
from invertio.api.app import create_app
from invertio.api.context import ApiContext
from invertio.api.hub import EventHub
from invertio.api.server import build_server, port_available
from invertio.config.app_config import AppConfig, read_yaml
from invertio.config.settings import Settings
from invertio.data.store import BarStore
from invertio.data.throttle import RequestGate
from invertio.experiments.execution_service import KEY as EXECUTION_KEY
from invertio.experiments.execution_service import ExecutionExperimentService
from invertio.experiments.service import KEY as EXPERIMENTS_KEY
from invertio.experiments.service import ExperimentsService
from invertio.experiments.timeframe_service import KEY as TIMEFRAME_KEY
from invertio.experiments.timeframe_service import TimeframeExperimentsService
from invertio.manual.client import RevolutXClient
from invertio.manual.repository import ManualRepository
from invertio.manual.service import ManualService
from invertio.notify.telegram import TelegramClient
from invertio.persistence.db import create_engine_async, session_factory, upgrade_db
from invertio.simulation.config import SimulationConfig
from invertio.simulation.repository import SimulationRepository
from invertio.simulation.service import SimulationService


@contextlib.contextmanager
def analysis_lock(data_dir: Path) -> Iterator[None]:
    """Un único escritor/notificador de análisis por base, incluso sin panel."""
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / "analysis.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("Ya hay un análisis usando esta carpeta de datos") from None
        try:
            yield
        finally:
            handle.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


async def run_analysis(
    settings: Settings,
    app_config: AppConfig,
    config: AnalysisConfig,
    *,
    panel: bool = True,
    telegram_enabled: bool = True,
    max_cycles: int | None = None,
    manual_orders: bool = False,
    simulate: bool = False,
    compare_strategies: bool = False,
    execution_trial: bool = False,
    timeframe_trial: bool = False,
    warn: Callable[[str], None] = print,
) -> None:
    if panel and not port_available(settings.panel_port):
        raise ValueError(
            f"El puerto {settings.panel_port} está ocupado; el análisis no ha arrancado"
        )
    if manual_orders and not panel:
        raise ValueError("Las órdenes manuales requieren el panel")
    if config.market_scope == "all_eur" and not (
        settings.revolutx_api_key and settings.revolutx_private_key_path
    ):
        raise ValueError(
            "Observar todos los pares cada minuto requiere configurar las claves API "
            "de Revolut X para consultar datos; estas consultas no envían órdenes"
        )
    with analysis_lock(settings.data_dir):
        upgrade_db(settings)
        db = create_engine_async(settings)
        repository = AnalysisRepository(session_factory(db))
        telegram: TelegramClient | None = None
        service: AnalysisService | None = None
        server = None
        server_task: asyncio.Task[None] | None = None
        notification_task: asyncio.Task[None] | None = None
        manual: ManualService | None = None
        stop = asyncio.Event()
        hub = EventHub()
        request_gate = RequestGate()
        try:
            sender = None
            if telegram_enabled and settings.telegram_bot_token and settings.telegram_chat_id:
                telegram = TelegramClient(settings.telegram_bot_token.get_secret_value())
                chat_id = settings.telegram_chat_id

                async def send(text: str) -> None:
                    assert telegram is not None
                    await telegram.send_message(chat_id, text)

                sender = send
            else:
                warn(
                    "Telegram desactivado o sin configurar; "
                    "el panel y el historial siguen disponibles"
                )
            feed: AnalysisFeed
            if config.market_scope == "all_eur":
                from invertio.analysis.authenticated_feed import AuthenticatedAnalysisFeed

                assert settings.revolutx_api_key and settings.revolutx_private_key_path
                market_client = RevolutXClient(
                    settings.revolutx_api_key.get_secret_value(),
                    settings.revolutx_private_key_path.read_bytes(),
                    request_interval=0.2,
                )
                feed = AuthenticatedAnalysisFeed(config, market_client, request_gate=request_gate)
            else:
                feed = PublicAnalysisFeed(config, request_gate=request_gate)
            service = AnalysisService(
                config,
                feed,
                repository,
                BarStore(settings.data_dir / "bars"),
                dispatcher=NotificationDispatcher(repository, sender=sender),
                telegram_configured=sender is not None,
                broadcast=hub.broadcast,
            )
            if simulate:
                simulation_path = settings.config_dir / "simulation.yaml"
                simulation_config = (
                    SimulationConfig.model_validate(read_yaml(simulation_path))
                    if simulation_path.exists()
                    else SimulationConfig()
                )
                service.simulation = SimulationService(
                    simulation_config,
                    SimulationRepository(session_factory(db)),
                    service.feed,
                )
                await service.simulation.start()
            if compare_strategies:
                service.experiments = ExperimentsService(
                    SimulationRepository(session_factory(db), key=EXPERIMENTS_KEY),
                    service.feed,
                )
                await service.experiments.start()
            if execution_trial:
                service.execution_experiment = ExecutionExperimentService(
                    SimulationRepository(session_factory(db), key=EXECUTION_KEY), service.feed
                )
                await service.execution_experiment.start()
            if timeframe_trial:
                service.timeframe_experiment = TimeframeExperimentsService(
                    SimulationRepository(session_factory(db), key=TIMEFRAME_KEY),
                    service.feed,
                    store=service.store,
                )
                await service.timeframe_experiment.start()

            async def deliver() -> None:
                assert service is not None and service.dispatcher is not None
                while not stop.is_set():
                    await service.dispatcher.flush()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=5)

            notification_task = asyncio.create_task(deliver(), name="analysis-notifications")
            if panel:
                client = None
                setup_error = None
                account_id = "unconfigured"
                if settings.revolutx_api_key and settings.revolutx_private_key_path:
                    try:
                        key = settings.revolutx_api_key.get_secret_value()
                        client = RevolutXClient(
                            key,
                            settings.revolutx_private_key_path.read_bytes(),
                            request_gate=request_gate,
                        )
                        account_id = hashlib.sha256(key.encode()).hexdigest()
                    except Exception:
                        setup_error = "No se pudieron cargar las claves API/Ed25519 de Revolut X"
                manual = ManualService(
                    ManualRepository(session_factory(db)),
                    client,
                    enabled=manual_orders,
                    account_id=account_id,
                    setup_error=setup_error,
                )
                context = ApiContext(
                    settings,
                    app_config,
                    None,
                    session_factory(db),
                    service.store,
                    analysis=service,
                    manual=manual,
                    simulation=service.simulation,
                    experiments=service.experiments,
                    execution_experiment=service.execution_experiment,
                    timeframe_experiment=service.timeframe_experiment,
                )
                server = build_server(create_app(context, hub), settings.panel_port)
                server_task = asyncio.create_task(server.serve(), name="analysis-panel")
                while not server.started:
                    if server_task.done():
                        await server_task
                        raise ValueError("No se pudo arrancar el panel")
                    await asyncio.sleep(0.05)
            await service.run(stop, max_cycles=max_cycles)
        finally:
            stop.set()
            hub.close_all()
            if server is not None:
                server.should_exit = True
            if server_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await server_task
            if service is not None:
                await service.close()
            if manual is not None:
                await manual.close()
            if notification_task is not None:
                notification_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await notification_task
            if telegram is not None:
                await telegram.close()
            await db.dispose()
