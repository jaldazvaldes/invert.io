"""Estado compartido por las rutas de la API del panel."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from invertio.config.app_config import AppConfig
from invertio.config.live_config import LiveConfig
from invertio.config.settings import Settings
from invertio.data.store import BarStore

if TYPE_CHECKING:
    from invertio.analysis.service import AnalysisService
    from invertio.experiments.service import ExperimentsService
    from invertio.live.engine import LiveEngine
    from invertio.manual.service import ManualService
    from invertio.simulation.service import SimulationService


@dataclass(slots=True)
class ScanState:
    status: str = "idle"  # idle | running | done | error
    timeframe: str | None = None
    all_markets: bool = False
    progress: dict[str, tuple[int, int]] = field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None
    task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class ApiContext:
    settings: Settings
    config: AppConfig
    live: LiveConfig | None
    sessions: async_sessionmaker[AsyncSession]
    store: BarStore
    engine: LiveEngine | None = None
    analysis: AnalysisService | None = None
    manual: ManualService | None = None
    simulation: SimulationService | None = None
    experiments: ExperimentsService | None = None
    execution_experiment: ExperimentsService | None = None
    timeframe_experiment: ExperimentsService | None = None
    lab_experiment: ExperimentsService | None = None
    learned_experiment: ExperimentsService | None = None
    # Token de sesión: las acciones (pausa, pánico…) lo exigen en una cabecera. Otra web abierta
    # en el navegador no puede leerlo ni enviarlo, así que no puede pulsar botones por ti.
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    scan: ScanState = field(default_factory=ScanState)

    @property
    def timeframe(self) -> str:
        return self.engine.timeframe if self.engine else self.config.data.timeframe
