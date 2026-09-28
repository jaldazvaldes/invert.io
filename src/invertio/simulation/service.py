"""Coordinación del simulador: datos públicos compartidos y persistencia, sin bróker."""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from invertio.analysis.market import AnalysisFeed
from invertio.analysis.tracking import timestamp
from invertio.core.clock import Clock, LiveClock
from invertio.simulation.config import SimulationConfig
from invertio.simulation.engine import SimulationEngine
from invertio.simulation.repository import SimulationRepository


class SimulationService:
    def __init__(
        self,
        config: SimulationConfig,
        repository: SimulationRepository,
        feed: AnalysisFeed,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.config, self.repository, self.feed = config, repository, feed
        self.clock = clock or LiveClock()
        self.engine: SimulationEngine | None = None
        self.last_error: str | None = None

    async def start(self) -> None:
        if self.engine is not None:
            return
        saved = await self.repository.load()
        self.engine = SimulationEngine(self.config, saved, self.clock.now())
        if saved is None:
            # Guardar la fecha de activación antes de producir señales del primer ciclo.
            await self.repository.save(self.engine.state)

    def holding_symbols(self) -> list[str]:
        return self.engine.holding_symbols() if self.engine else []

    async def cycle(
        self,
        *,
        rows: list[dict[str, Any]],
        opportunities: list[dict[str, Any]],
        books: dict[str, dict[str, Any]],
        markets: dict[str, dict[str, Any]],
        analysis_cycle: int,
    ) -> None:
        try:
            await self.start()
            assert self.engine is not None
            saved = copy.deepcopy(self.engine.state)
            if analysis_cycle <= int(saved.get("analysis_cycle", -1)):
                return
            candidate_engine = SimulationEngine(self.config, saved, self.clock.now())
            started = timestamp(saved["started_at"])
            seen = set(saved["seen_opportunity_ids"])
            candidates = sorted(
                (
                    p
                    for p in opportunities
                    if p["status"] == "open"
                    and p["id"] not in seen
                    and timestamp(p["created_at"]) >= started
                ),
                key=lambda p: (p["created_at"], p["id"]),
            )
            # No duplica catálogos, tickers ni velas. Solo completa los libros necesarios
            # para la cartera realista de 10 €, compartiendo el mismo cliente/limitador.
            symbols = list(
                dict.fromkeys(
                    candidate_engine.holding_symbols()
                    + [p["market"].split(":", 1)[1] for p in candidates]
                )
            )
            current_books: dict[str, dict[str, Any]] = {}
            for symbol in symbols:
                book = books.get(symbol)
                try:
                    age = (
                        (self.clock.now() - timestamp(book["ts"])).total_seconds() if book else 999
                    )
                    if not 0 <= age <= 5:
                        book = await self.feed.book(symbol)
                    if book is not None:
                        current_books[symbol] = book
                        books[symbol] = book
                except Exception:
                    # El motor conserva posiciones y efectivo hasta recuperar un libro válido.
                    continue
            now = self.clock.now()
            snapshot = candidate_engine.step(rows, opportunities, current_books, markets, now)
            snapshot["analysis_cycle"] = analysis_cycle
            await self.repository.save(snapshot)
            # Publicar solo después del commit; fallar no deja débitos solo en memoria.
            self.engine = SimulationEngine(self.config, snapshot, now)
            self.last_error = None
        except Exception as exc:
            self.last_error = f"Simulación pendiente de recuperar: {type(exc).__name__}"

    def status(self, running: bool, *, now: datetime | None = None) -> dict[str, Any]:
        snapshot = copy.deepcopy(self.engine.state) if self.engine else {}
        last = snapshot.get("last_cycle_at")
        stale = bool(last and ((now or self.clock.now()) - timestamp(last)).total_seconds() > 120)
        waiting = bool(snapshot.get("stats", {}).get("waiting_positions", 0))
        state = "running" if last else "starting"
        if not running:
            state = "stopped"
        elif self.last_error:
            state = "error"
        elif waiting or stale:
            state = "waiting_data"
        if stale and snapshot.get("positions"):
            snapshot["stats"].update(equity_eur=None, unrealized_pnl_eur=None, return_pct=None)
        return {
            **snapshot,
            "available": self.engine is not None,
            "running": running,
            "state": state,
            "last_error": self.last_error,
            "valuation_stale": stale,
        }
