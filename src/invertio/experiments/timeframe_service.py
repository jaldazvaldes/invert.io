"""Grupo de estrategias con velas de 10 minutos y de una hora; nunca envía órdenes reales.

Cada minuto recibe el mismo ciclo que el resto de comparaciones: cotizaciones,
velas de un minuto y libros. Las velas largas se construyen con las velas de un
minuto que el análisis ya ha descargado (coinciden con las nativas de Revolut X),
así que en marcha normal no hacen falta peticiones nuevas. Sólo al arrancar o
tras un hueco se piden velas nativas, con un tope de tiempo por ciclo para no
alargar el análisis del resto de carteras.
"""

from __future__ import annotations

import asyncio
import copy
import time
from datetime import datetime, timedelta
from typing import Any

from invertio.analysis.market import AnalysisFeed
from invertio.core.clock import Clock
from invertio.core.models import Bar
from invertio.core.timeframes import floor_to_timeframe, timeframe_delta
from invertio.data.store import BarStore
from invertio.experiments.service import ExperimentsService
from invertio.experiments.timeframe_policies import (
    NATIVE_TIMEFRAMES,
    VERSION,
    definitions,
    evaluate,
    native_timeframe,
)
from invertio.simulation.repository import SimulationRepository

KEY = "timeframes-v1"
HISTORY_CANDLES = 1000


class TimeframeExperimentsService(ExperimentsService):
    name = "Velas de 10 minutos y 1 hora"
    identity_prefix = "timeframes"
    signal_version = VERSION
    # Tiempo máximo de descargas por ciclo; el resto sigue en el ciclo siguiente.
    refresh_budget_seconds = 15.0

    def __init__(
        self,
        repository: SimulationRepository,
        feed: AnalysisFeed,
        *,
        clock: Clock | None = None,
        store: BarStore | None = None,
    ) -> None:
        super().__init__(repository, feed, clock=clock)
        self.store = store
        self._native: dict[tuple[str, str], list[Bar]] = {}
        self._loaded: set[tuple[str, str]] = set()
        self.data_errors: dict[str, str] = {}
        self._refreshed_cycle = -1
        self._attempted: dict[tuple[str, str], float] = {}

    def _catalog(self) -> list[dict[str, Any]]:
        return definitions()

    def _policy(self, ident: str, bars: list[Bar], now: datetime) -> dict[str, Any]:
        # ``bars`` son las velas de un minuto del análisis: dan el mercado y la frescura.
        native = self._native.get((bars[-1].symbol, native_timeframe(ident)), []) if bars else []
        return evaluate(ident, native, bars, now)

    def _stable_policy(self, policy: dict[str, Any], cursor: dict[str, Any]) -> dict[str, Any]:
        # Una vela larga ya cerrada decide una sola vez; la frescura de un minuto
        # y la preparación actual siguen mandando en cada ciclo.
        if not policy["ready"] or policy.get("signal_bar_ts") is None:
            return policy
        saved = cursor.get("signal_snapshot")
        if saved is not None and saved["signal_bar_ts"] == policy["signal_bar_ts"]:
            stable: dict[str, Any] = copy.deepcopy(saved)
            stable["current_bar_ts"] = policy["current_bar_ts"]
            return stable
        cursor["signal_snapshot"] = copy.deepcopy(policy)
        return policy

    async def _stored(self, symbol: str, timeframe: str) -> list[Bar]:
        if self.store is None:
            return []
        try:
            return await asyncio.to_thread(
                self.store.read,
                "revolutx",
                symbol,
                timeframe,
                venue="revolutx",
                last=HISTORY_CANDLES,
            )
        except FileNotFoundError:
            return []

    def _current(self, key: tuple[str, str], now: datetime) -> bool:
        """¿Está la última vela nativa del último bloque completo del periodo de señal?"""
        cached = self._native.get(key)
        if not cached:
            return False
        timeframe = key[1]
        minutes = NATIVE_TIMEFRAMES[timeframe]
        expected = floor_to_timeframe(now, f"{minutes}m") - timeframe_delta(timeframe)
        return cached[-1].open_time >= expected

    def extend(self, bars: dict[str, list[Bar]]) -> None:
        """Continúa cada serie larga con bloques completos de velas de un minuto, sin red."""
        now = self.clock.now()
        minute = timedelta(minutes=1)
        for key, cached in list(self._native.items()):
            symbol, timeframe = key
            if not cached:
                continue
            step = timeframe_delta(timeframe)
            count = step // minute
            by_time = {
                bar.open_time: bar
                for bar in bars.get(symbol, [])
                if bar.timeframe == "1m" and bar.close_time <= now
            }
            start = cached[-1].open_time + step
            added: list[Bar] = []
            while start + step <= now:
                parts = [
                    bar
                    for index in range(count)
                    if (bar := by_time.get(start + index * minute)) is not None
                ]
                if len(parts) != count:
                    break
                added.append(
                    Bar(
                        venue=parts[0].venue,
                        symbol=symbol,
                        timeframe=timeframe,
                        open_time=start,
                        open=parts[0].open,
                        high=max(bar.high for bar in parts),
                        low=min(bar.low for bar in parts),
                        close=parts[-1].close,
                        volume=sum(bar.volume for bar in parts),
                    )
                )
                start += step
            if added:
                self._native[key] = (cached + added)[-HISTORY_CANDLES:]

    async def load(self, symbols: list[str]) -> None:
        """Recupera del disco las series largas aún no cargadas; no usa la red."""
        for timeframe in NATIVE_TIMEFRAMES:
            for symbol in symbols:
                key = (symbol, timeframe)
                if key not in self._loaded:
                    self._native[key] = await self._stored(symbol, timeframe)
                    self._loaded.add(key)

    async def refresh(self, symbols: list[str]) -> None:
        """Descarga las series que no se han podido continuar, con tope de tiempo por ciclo."""
        deadline = time.monotonic() + self.refresh_budget_seconds
        await self.load(symbols)
        pending = [
            (symbol, timeframe)
            for timeframe in NATIVE_TIMEFRAMES
            for symbol in symbols
            if not self._current((symbol, timeframe), self.clock.now())
        ]
        # Primero las que llevan más tiempo sin intentarse: el tope no deja ninguna atrás.
        pending.sort(key=lambda key: self._attempted.get(key, 0.0))
        for key in pending:
            if time.monotonic() >= deadline:
                break
            self._attempted[key] = time.monotonic()
            symbol, timeframe = key
            now = self.clock.now()
            step = timeframe_delta(timeframe)
            cached = self._native[key]
            oldest = now - step * HISTORY_CANDLES
            since = max(cached[-1].close_time, oldest) if cached else oldest
            try:
                fresh = await self.feed.bars(symbol, since, now, timeframe=timeframe)
            except Exception as exc:
                self.data_errors[f"{symbol} {timeframe}"] = type(exc).__name__
                continue
            self.data_errors.pop(f"{symbol} {timeframe}", None)
            fresh = [bar for bar in fresh if bar.timeframe == timeframe and bar.close_time <= now]
            if fresh and self.store is not None:
                await asyncio.to_thread(self.store.write, "revolutx", symbol, timeframe, fresh)
            merged = {bar.open_time: bar for bar in cached + fresh}
            self._native[key] = sorted(merged.values(), key=lambda bar: bar.open_time)[
                -HISTORY_CANDLES:
            ]

    async def cycle(
        self,
        *,
        rows: list[dict[str, Any]],
        bars: dict[str, list[Bar]],
        books: dict[str, dict[str, Any]],
        markets: dict[str, dict[str, Any]],
        analysis_cycle: int,
    ) -> None:
        symbols = sorted(
            {row["market"].split(":", 1)[1] for row in rows} | set(self.holding_symbols())
        )
        # Sin red: lo guardado en disco y las velas de un minuto del análisis bastan
        # para decidir ya en el primer ciclo tras un reinicio, y una vela larga recién
        # cerrada se usa en este mismo ciclo.
        try:
            await self.load(symbols)
        except Exception as exc:
            self.data_errors["load"] = type(exc).__name__
        self.extend(bars)
        await super().cycle(
            rows=rows, bars=bars, books=books, markets=markets, analysis_cycle=analysis_cycle
        )
        if analysis_cycle <= self._refreshed_cycle:
            return
        self._refreshed_cycle = analysis_cycle
        # Las descargas van después de decidir: las cotizaciones de este ciclo siguen
        # frescas para las carteras y lo descargado se usa en el siguiente.
        try:
            await self.refresh(symbols)
        except Exception as exc:
            # Sin velas nuevas las reglas se quedan sin preparar; stops, objetivos y
            # caducidades se siguen vigilando con los libros en cada ciclo.
            self.data_errors["refresh"] = type(exc).__name__

    def status(self, running: bool) -> dict[str, Any]:
        result = super().status(running)
        result["data_errors"] = len(self.data_errors)
        result["history_ready"] = {
            timeframe: sum(
                1
                for (_, tf), series in self._native.items()
                if tf == timeframe
                and series
                and self.clock.now() - series[-1].close_time
                <= timeframe_delta(f"{minutes}m") + timedelta(minutes=5)
            )
            for timeframe, minutes in NATIVE_TIMEFRAMES.items()
        }
        return result
