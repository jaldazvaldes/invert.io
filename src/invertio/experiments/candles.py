"""Carteras ficticias que deciden con velas largas (10 min, 1 h, 4 h, 1 día…).

Cada minuto reciben el mismo ciclo que el resto de comparaciones: cotizaciones, velas de
un minuto y libros. Las velas largas se guardan por (mercado, vela nativa de Revolut X):
- al arrancar se leen del disco, sin red, para decidir ya en el primer ciclo;
- en marcha se construyen con las velas de un minuto que el análisis ya ha descargado
  (coinciden con las nativas), así que no hacen falta peticiones nuevas;
- solo si falta alguna (arranque, hueco, velas diarias) se piden a Revolut X, después de
  decidir y con un tope de tiempo por ciclo para no alargar el análisis del resto.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import time
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any, ClassVar

from invertio.analysis.market import AnalysisFeed
from invertio.core.clock import Clock
from invertio.core.models import Bar
from invertio.core.timeframes import floor_to_timeframe, timeframe_delta
from invertio.data.store import BarStore
from invertio.experiments.service import ExperimentsService
from invertio.simulation.repository import SimulationRepository

HISTORY_CANDLES = 1000


class LongCandleExperiments(ExperimentsService):
    # Vela nativa de Revolut X -> minutos del periodo de señal que la usa.
    native_timeframes: ClassVar[dict[str, int]] = {}
    # Tiempo máximo de descargas por ciclo; el resto sigue en el ciclo siguiente.
    refresh_budget_seconds = 15.0
    # Velas guardadas por serie (las descargas piden páginas de 1000).
    history_candles: ClassVar[int] = HISTORY_CANDLES

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
        # Huecos (vela anterior) que ni Revolut X tiene: no se piden otra vez.
        self._unfillable: dict[tuple[str, str], set[datetime]] = {}

    def native(self, symbol: str, timeframe: str) -> list[Bar]:
        return self._native.get((symbol, timeframe), [])

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
                last=self.history_candles,
            )
        except FileNotFoundError:
            return []

    def _current(self, key: tuple[str, str], now: datetime) -> bool:
        """¿Está la última vela nativa del último bloque completo del periodo de señal?"""
        cached = self._native.get(key)
        if not cached:
            return False
        timeframe = key[1]
        minutes = self.native_timeframes[timeframe]
        expected = floor_to_timeframe(now, f"{minutes}m") - timeframe_delta(timeframe)
        return cached[-1].open_time >= expected

    def _missing_since(self, key: tuple[str, str], now: datetime) -> datetime | None:
        """Desde cuándo pedir velas: el primer hueco de la ventana o el final; None si nada.

        Las reglas necesitan velas consecutivas: un hueco en medio las deja sin preparar
        aunque la última vela esté al día. Un hueco que Revolut X tampoco tiene se anota y
        no se vuelve a pedir en cada ciclo.
        """
        cached = self._native.get(key)
        step = timeframe_delta(key[1])
        oldest = now - step * self.history_candles
        if not cached:
            return oldest
        unfillable = self._unfillable.get(key, set())
        for prev, bar in pairwise(cached):
            if (
                bar.open_time - prev.open_time != step
                and prev.close_time >= oldest
                and prev.open_time not in unfillable
            ):
                return prev.close_time
        return None if self._current(key, now) else max(cached[-1].close_time, oldest)

    def extend(self, bars: dict[str, list[Bar]]) -> dict[tuple[str, str], list[Bar]]:
        """Continúa cada serie larga con bloques completos de velas de un minuto, sin red.

        Devuelve las velas añadidas para guardarlas: si solo vivieran en memoria, tras un
        reinicio largo el disco no llegaría hasta las velas de un minuto disponibles.
        """
        now = self.clock.now()
        minute = timedelta(minutes=1)
        result: dict[tuple[str, str], list[Bar]] = {}
        for key, cached in list(self._native.items()):
            symbol, timeframe = key
            step = timeframe_delta(timeframe)
            count = step // minute
            if not cached or count > len(bars.get(symbol, [])):
                continue  # p. ej. velas diarias: el análisis guarda menos de 1440 minutos
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
                self._native[key] = (cached + added)[-self.history_candles :]
                result[key] = added
        return result

    async def _persist(self, added: dict[tuple[str, str], list[Bar]]) -> None:
        if self.store is None:
            return
        for (symbol, timeframe), bars in added.items():
            await asyncio.to_thread(self.store.write, "revolutx", symbol, timeframe, bars)

    async def load(self, symbols: list[str]) -> None:
        """Recupera del disco las series largas aún no cargadas; no usa la red."""
        for timeframe in self.native_timeframes:
            for symbol in symbols:
                key = (symbol, timeframe)
                if key not in self._loaded:
                    self._native[key] = await self._stored(symbol, timeframe)
                    self._loaded.add(key)

    async def refresh(self, symbols: list[str]) -> None:
        """Descarga lo que falta (huecos o velas nuevas), con tope de tiempo por ciclo."""
        deadline = time.monotonic() + self.refresh_budget_seconds
        await self.load(symbols)
        pending = [
            (symbol, timeframe)
            for timeframe in self.native_timeframes
            for symbol in symbols
            if self._missing_since((symbol, timeframe), self.clock.now()) is not None
        ]
        # Primero las que llevan más tiempo sin intentarse: el tope no deja ninguna atrás.
        pending.sort(key=lambda key: self._attempted.get(key, 0.0))
        for key in pending:
            if time.monotonic() >= deadline:
                break
            self._attempted[key] = time.monotonic()
            symbol, timeframe = key
            now = self.clock.now()
            since = self._missing_since(key, now)
            if since is None:
                continue
            cached = self._native[key]
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
                -self.history_candles :
            ]
            # Si el hueco pedido sigue ahí, Revolut X no tiene esas velas: no insistir.
            gap = next(
                (
                    prev.open_time
                    for prev, bar in pairwise(self._native[key])
                    if prev.close_time == since and bar.open_time != since
                ),
                None,
            )
            if gap is not None:
                self._unfillable.setdefault(key, set()).add(gap)

    def before_decisions(self, symbols: list[str], now: datetime) -> None:
        """Gancho sin red tras actualizar las velas y antes de que decidan las carteras."""

    async def cycle(
        self,
        *,
        rows: list[dict[str, Any]],
        bars: dict[str, list[Bar]],
        books: dict[str, dict[str, Any]],
        markets: dict[str, dict[str, Any]],
        analysis_cycle: int,
    ) -> None:
        # Con el estado cargado se sabe qué posiciones hay abiertas antes de decidir.
        with contextlib.suppress(Exception):  # si falla, el ciclo base lo registra
            await self.start()
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
        added = self.extend(bars)
        try:
            await self._persist(added)
        except Exception as exc:
            self.data_errors["persist"] = type(exc).__name__
        # Una posición abierta sin velas al día o con huecos (p. ej. tras un reinicio largo)
        # se vendería por falta de datos: se descargan antes de decidir, con el mismo tope.
        now = self.clock.now()
        stale = [
            symbol
            for symbol in self.holding_symbols()
            if any(
                self._missing_since((symbol, tf), now) is not None for tf in self.native_timeframes
            )
        ]
        if stale:
            try:
                await self.refresh(stale)
            except Exception as exc:
                self.data_errors["refresh"] = type(exc).__name__
        self.before_decisions(symbols, self.clock.now())
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
            for timeframe, minutes in self.native_timeframes.items()
        }
        return result
