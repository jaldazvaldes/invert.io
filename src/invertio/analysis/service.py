"""Recogida de datos y oportunidades, sin dependencias del motor de ejecución."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import math
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from invertio.analysis.config import AnalysisConfig
from invertio.analysis.market import (
    AnalysisFeed,
    estimate_costs,
    estimate_tracking_costs,
    select_markets,
)
from invertio.analysis.notifications import NotificationDispatcher, format_end, format_start
from invertio.analysis.repository import AnalysisRepository
from invertio.analysis.tracking import HORIZONS, finish, timestamp, track_bars
from invertio.core.clock import Clock, LiveClock
from invertio.core.models import Bar
from invertio.data.store import BarStore
from invertio.live.feeds import STABLECOINS
from invertio.strategies.score import ScoreResult, ScoreStrategy

log = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from invertio.experiments.service import ExperimentsService
    from invertio.simulation.service import SimulationService
_NO_TRADES_REASON = "La última vela no acredita volumen negociado"


class AnalysisService:
    def __init__(
        self,
        config: AnalysisConfig,
        feed: AnalysisFeed,
        repository: AnalysisRepository,
        store: BarStore,
        *,
        clock: Clock | None = None,
        dispatcher: NotificationDispatcher | None = None,
        broadcast: Callable[[dict[str, Any]], None] | None = None,
        telegram_configured: bool = False,
        simulation: SimulationService | None = None,
        experiments: ExperimentsService | None = None,
        execution_experiment: ExperimentsService | None = None,
        timeframe_experiment: ExperimentsService | None = None,
    ) -> None:
        self.config, self.feed, self.repository, self.store = config, feed, repository, store
        self.clock = clock or LiveClock()
        self.dispatcher, self.broadcast = dispatcher, broadcast
        self.telegram_configured = telegram_configured
        self.simulation = simulation
        self.experiments = experiments
        self.execution_experiment = execution_experiment
        self.timeframe_experiment = timeframe_experiment
        self._cycle_books: dict[str, dict[str, Any]] = {}
        snapshot = config.model_dump(mode="json")
        # Ampliar el universo no cambia las reglas ni termina señales ya abiertas.
        snapshot.pop("market_scope", None)
        self.rule_version = (
            "score-1m-v3:"
            + hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()[:12]
        )
        self.running = False
        self.state = "starting"
        self.cycles = 0
        self.last_cycle_at: str | None = None
        self.last_error: str | None = None
        self.selected: list[str] = []
        self.excluded: list[dict[str, Any]] = []
        self.rows: list[dict[str, Any]] = []
        self._states: dict[str, dict[str, Any]] = {}
        self._opportunities: dict[str, dict[str, Any]] = {}
        self._bars: dict[str, list[Bar]] = {}
        self._strategies: dict[str, ScoreStrategy] = {}
        self._evaluated: dict[str, datetime] = {}
        self._scores: dict[str, ScoreResult] = {}
        self._markets: dict[str, dict[str, Any]] = {}
        self._selection_at: datetime | None = None
        self._cycle_lock = asyncio.Lock()
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._states = await self.repository.load_state()
        saved = self._states.get("service", {})
        self.cycles = int(saved.get("cycles", 0))
        self.last_cycle_at = saved.get("last_cycle_at")
        recent = await self.repository.opportunities(since=self.clock.now() - timedelta(days=1))
        opened = await self.repository.opportunities(status="open")
        self._opportunities = {p["id"]: p for p in recent + opened}
        self.running = self._started = True

    def _event(self) -> None:
        if self.broadcast is not None:
            self.broadcast({"type": "analysis", "state": self.state})

    def coverage(self) -> dict[str, Any]:
        rejected = {r["symbol"] for r in self.excluded} & set(self.selected)
        catalog_eur = sum(
            m.get("quote") == "EUR" and bool(m.get("spot")) and m.get("active") is True
            for m in self._markets.values()
        )
        return {
            "scope": self.config.market_scope,
            "catalog_eur": getattr(self.feed, "market_verification", {}).get(
                "public_eur", catalog_eur
            ),
            "observed": len(self.selected),
            "entry_eligible": len(self.selected) - len(rejected),
            "excluded": len(rejected),
            "data_access": getattr(self.feed, "data_access", "public"),
            "request_interval_seconds": getattr(self.feed, "request_interval_seconds", 1.0),
        }

    async def status(self) -> dict[str, Any]:
        counts = await self.repository.notification_counts()
        return {
            "available": True,
            "running": self.running,
            "state": self.state,
            "timeframe": "1m",
            "interval_seconds": self.config.interval_seconds,
            "selected": self.selected,
            "coverage": self.coverage(),
            "warmup": {
                "ready": len(self._scores.keys() & set(self.selected)),
                "total": len(self.selected),
            },
            "cycles": self.cycles,
            "last_cycle_at": self.last_cycle_at,
            "last_error": self.last_error,
            "telegram": {"configured": self.telegram_configured, **counts},
            "config": self.config.model_dump(mode="json"),
        }

    async def _load_bars(self, symbol: str, *, history_start: datetime | None = None) -> list[Bar]:
        now = self.clock.now()
        if symbol not in self._bars:
            try:
                cached = await asyncio.to_thread(
                    self.store.read, "revolutx", symbol, "1m", venue="revolutx", last=600
                )
            except FileNotFoundError:
                cached = []
            self._bars[symbol] = cached
        cached = self._bars[symbol]
        start = now - timedelta(minutes=self.config.warmup_bars + 30)
        if history_start is not None:
            start = history_start
        elif cached:
            start = max(start, cached[-1].close_time)
        fresh = await self.feed.bars(symbol, start, now)
        fresh = [b for b in fresh if b.close_time <= now and b.timeframe == "1m"]
        if fresh:
            await asyncio.to_thread(self.store.write, "revolutx", symbol, "1m", fresh)
        merged = {b.open_time: b for b in cached + fresh if b.close_time <= now}
        result = sorted(merged.values(), key=lambda b: b.open_time)[-600:]
        self._bars[symbol] = result
        return result

    def _score(self, symbol: str, bars: list[Bar]) -> ScoreResult | None:
        strategy = self._strategies.setdefault(symbol, ScoreStrategy(self.config.score_params))
        last = self._evaluated.get(symbol)
        for bar in bars:
            if last is not None and bar.close_time <= last:
                continue
            if last is not None and (bar.close_time - last).total_seconds() > 60:
                # No llamar "200 minutos" a 200 velas separadas por un apagado largo.
                strategy = ScoreStrategy(self.config.score_params)
                self._strategies[symbol] = strategy
                self._scores.pop(symbol, None)
            result = strategy.evaluate(bar)
            self._evaluated[symbol] = last = bar.close_time
            if result is not None:
                self._scores[symbol] = result
        return self._scores.get(symbol)

    async def _recover_horizons(self, opportunity: dict[str, Any]) -> None:
        created = timestamp(opportunity["created_at"])
        end = min(self.clock.now(), created + timedelta(hours=4, minutes=1))
        symbol = opportunity["market"].split(":", 1)[1]
        # Ventana limitada a esta oportunidad, no los últimos 600 minutos del mercado.
        recovered = await self.feed.bars(symbol, created, end)
        recovered = [b for b in recovered if created <= b.open_time and b.close_time <= end]
        if recovered:
            await asyncio.to_thread(self.store.write, "revolutx", symbol, "1m", recovered)
        try:
            bars = await asyncio.to_thread(
                self.store.read, "revolutx", symbol, "1m", venue="revolutx", start=created, end=end
            )
        except FileNotFoundError:
            bars = []
        replay = copy.deepcopy(opportunity)
        replay["last_tracked_at"] = opportunity["created_at"]
        replay["horizons"] = {
            name: {"at": None, "return_pct": None, "net_return_pct": None, "quality": "pending"}
            for name in HORIZONS
        }
        track_bars(replay, bars, self.clock.now())
        for name, minutes in HORIZONS.items():
            if opportunity["horizons"][name]["at"] is not None:
                continue
            opportunity["horizons"][name] = replay["horizons"][name]
            due = created + timedelta(minutes=minutes)
            if self.clock.now() >= due + timedelta(minutes=2):
                horizon = opportunity["horizons"][name]
                if horizon["at"] is None:
                    horizon.update(at=due.isoformat(), quality="incomplete")
        for key in ("high_water", "low_water", "mfe_pct", "mae_pct"):
            opportunity[key] = replay[key]

    def _fresh(self, snapshot: dict[str, Any], seconds: int | float) -> bool:
        try:
            age = (self.clock.now() - timestamp(snapshot["ts"])).total_seconds()
            return 0 <= age <= seconds
        except (KeyError, TypeError, ValueError):
            return False

    def _open_for(self, symbol: str) -> dict[str, Any] | None:
        return next(
            (
                p
                for p in self._opportunities.values()
                if p["market"] == f"revolutx:{symbol}" and p["status"] == "open"
            ),
            None,
        )

    def _new_opportunity(self, row: dict[str, Any], now: datetime) -> dict[str, Any]:
        costs = row["costs"]
        identity = f"{row['market']}:{row['bar_ts']}:{self.rule_version}"
        return {
            "id": hashlib.sha256(identity.encode()).hexdigest()[:32],
            "market": row["market"],
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "bar_ts": row["bar_ts"],
            "status": "open",
            "outcome": None,
            "quality": "complete",
            "ended_at": None,
            "entry": costs["entry"],
            "stop": costs["stop"],
            "target": costs["target"],
            "quantity": costs["quantity"],
            "reference_eur": self.config.reference_eur,
            "score": row["score"],
            "points": row["points"],
            "max_points": row["max_points"],
            "positive": row["positive"],
            "negative": row["negative"],
            "costs": costs,
            "rule_version": self.rule_version,
            "config": self.config.model_dump(mode="json"),
            "high_water": costs["entry"],
            "low_water": costs["entry"],
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "horizons": {
                name: {"at": None, "return_pct": None, "net_return_pct": None, "quality": "pending"}
                for name in HORIZONS
            },
            "last_tracked_at": now.isoformat(),
            "tracking_gaps": False,
            "end_price": None,
            "estimated_return_pct": None,
            "reason": None,
        }

    def _notice(self, opportunity: dict[str, Any], kind: str) -> dict[str, Any]:
        now = self.clock.now()
        return {
            "id": f"{opportunity['id']}:{kind}",
            "opportunity_id": opportunity["id"],
            "kind": kind,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=60)).isoformat() if kind == "start" else None,
            "text": format_start(opportunity) if kind == "start" else format_end(opportunity),
        }

    async def _market_row(
        self, symbol: str, ticker: dict[str, Any], bars: list[Bar]
    ) -> dict[str, Any]:
        result = self._score(symbol, bars)
        now = self.clock.now()
        row: dict[str, Any] = {
            "market": f"revolutx:{symbol}",
            "bar_ts": bars[-1].close_time.isoformat() if bars else None,
            "observed_at": now.isoformat(),
            "rule_version": self.rule_version,
            "score": result.score if result else None,
            "points": result.points if result else {},
            "max_points": result.max_points if result else {},
            "positive": [],
            "negative": [],
            "reasons": [],
            "state": "watching" if result else "warming",
            "price": result.close if result else None,
            "atr_pct": result.atr_pct if result else None,
            "spread_pct": ticker.get("spread_pct"),
            "quote_volume": ticker.get("quote_volume"),
            "quote_ts": ticker.get("ts"),
            "costs": None,
            "opportunity_id": None,
        }
        reasons = row["reasons"]
        opened = self._open_for(symbol)
        tracking_costs = None
        market = self._markets.get(symbol, {})
        if market.get("base", symbol.split("/")[0]) in STABLECOINS:
            reasons.append("Stablecoin excluida de las entradas")
        if (
            market.get("active") is not True
            or not market.get("spot")
            or market.get("quote") != "EUR"
        ):
            reasons.append("Mercado inactivo o fuera del catálogo EUR al contado")
        if result is None:
            reasons.append(f"Preparando indicadores: {len(bars)}/{self.config.warmup_bars} velas")
        else:
            for name, points in result.points.items():
                maximum = result.max_points[name]
                if maximum <= 0:
                    continue
                label = f"{name.capitalize()}: {points:g}/{maximum:g} puntos"
                row["positive" if points >= maximum / 2 else "negative"].append(label)
            if result.score < self.config.entry_score:
                reasons.append(f"Nota inferior a {self.config.entry_score:g}")
        if not self._fresh(ticker, self.config.quote_max_age_seconds):
            reasons.append("Cotización ausente o antigua")
        if (
            not bars
            or (now - bars[-1].close_time).total_seconds() > self.config.bar_max_age_seconds
        ):
            reasons.append("Velas ausentes o antiguas")
        elif bars[-1].volume <= 0:
            reasons.append(_NO_TRADES_REASON)
        spread = ticker.get("spread_pct")
        if spread is None or not math.isfinite(spread) or spread > self.config.max_spread_pct:
            reasons.append("Spread desconocido o excesivo")
        volume = ticker.get("quote_volume")
        if volume is None or not math.isfinite(volume) or volume <= 0:
            reasons.append("Volumen en EUR desconocido o nulo")
        # Profundidad para entradas candidatas y para comprobar filtros de señales abiertas.
        if result is not None and (not reasons or opened is not None):
            book = await self.feed.book(symbol)
            self._cycle_books[symbol] = book
            if not self._fresh(book, self.config.quote_max_age_seconds):
                reasons.append("Profundidad ausente o antigua")
            else:
                row["costs"] = estimate_costs(book, self.config, result.atr)
                reasons.extend(row["costs"]["reasons"])
                if opened is not None:
                    tracking_costs = estimate_tracking_costs(book, opened)
        # Revalidar la edad después del I/O; no publicar una instantánea caducada en la cola.
        if (
            not self._fresh(ticker, self.config.quote_max_age_seconds)
            and "Cotización ausente o antigua" not in reasons
        ):
            reasons.append("Cotización ausente o antigua")
        if (
            bars
            and (self.clock.now() - bars[-1].close_time).total_seconds()
            > self.config.bar_max_age_seconds
            and "Velas ausentes o antiguas" not in reasons
        ):
            reasons.append("Velas ausentes o antiguas")
        if not reasons and row["costs"] and row["costs"]["eligible"]:
            row["state"] = "eligible"
        if opened is not None:
            # La viabilidad de una compra nueva sigue visible en reasons/costs.
            # Para salir se conserva el objetivo y la inversión de la señal abierta.
            row["exit_reasons"] = [
                reason
                for reason in reasons
                if reason != "Objetivo neto no positivo después de costes"
            ]
            row["tracking_costs"] = tracking_costs
            if tracking_costs is not None:
                row["exit_reasons"] = list(
                    dict.fromkeys(row["exit_reasons"] + tracking_costs["reasons"])
                )
        row["observed_at"] = self.clock.now().isoformat()
        return row

    async def cycle(self) -> None:
        if self._cycle_lock.locked():
            return
        async with self._cycle_lock:
            await self.start()
            self._cycle_books = {}
            changed: dict[str, dict[str, Any]] = {}
            notices: list[dict[str, Any]] = []
            rows: list[dict[str, Any]] = []
            errors: list[str] = []
            now = self.clock.now()
            previous_open = {p["id"] for p in self._opportunities.values() if p["status"] == "open"}
            for ident in previous_open:
                p = self._opportunities[ident]
                if p["rule_version"] != self.rule_version:
                    finish(
                        p,
                        "interrupted",
                        now,
                        "Configuración de análisis cambiada",
                        quality="incomplete",
                    )
                    changed[ident] = p
                    self._states.setdefault(p["market"], {})["armed"] = False
            # Apagados/red caída: no convertir precios recuperados en ejecuciones supuestas.
            if (
                self.last_cycle_at
                and (now - timestamp(self.last_cycle_at)).total_seconds()
                > self.config.bar_max_age_seconds
            ):
                for ident in previous_open:
                    p = self._opportunities[ident]
                    p["tracking_gaps"] = True
                    finish(
                        p,
                        "interrupted",
                        now,
                        "Seguimiento interrumpido por falta de datos",
                        quality="incomplete",
                    )
                    changed[ident] = p
            try:
                if (
                    not self._markets
                    or self._selection_at is None
                    or (now - self._selection_at).total_seconds() >= self.config.selection_seconds
                ):
                    self._markets = await self.feed.markets()
                tickers = await self.feed.tickers()
                if (
                    self._selection_at is None
                    or (now - self._selection_at).total_seconds() >= self.config.selection_seconds
                ):
                    held = [
                        p["market"].split(":", 1)[1]
                        for p in self._opportunities.values()
                        if p["status"] == "open"
                    ]
                    if self.simulation is not None:
                        held = list(dict.fromkeys(held + self.simulation.holding_symbols()))
                    if self.experiments is not None:
                        held = list(dict.fromkeys(held + self.experiments.holding_symbols()))
                    if self.execution_experiment is not None:
                        held = list(dict.fromkeys(
                            held + self.execution_experiment.holding_symbols()
                        ))
                    if self.timeframe_experiment is not None:
                        held = list(dict.fromkeys(
                            held + self.timeframe_experiment.holding_symbols()
                        ))
                    self.selected, self.excluded = select_markets(
                        self._markets, tickers, self.config, held
                    )
                    self._selection_at = now
                self._event()
                for symbol in self.selected:
                    try:
                        bars = await self._load_bars(symbol)
                        row = await self._market_row(symbol, tickers.get(symbol, {}), bars)
                        opened = self._open_for(symbol)
                        stale = any("antigua" in r or "ausente" in r for r in row["reasons"])
                        if opened is not None and stale:
                            opened["tracking_gaps"] = True
                            finish(
                                opened,
                                "interrupted",
                                self.clock.now(),
                                "Datos no disponibles",
                                quality="incomplete",
                            )
                            changed[opened["id"]] = opened
                        for p in list(self._opportunities.values()):
                            if p["market"] != row["market"]:
                                continue
                            if p["status"] == "open" or any(
                                h["at"] is None for h in p["horizons"].values()
                            ):
                                track_bars(p, bars, self.clock.now())
                                changed[p["id"]] = p
                        state = self._states.setdefault(row["market"], {"armed": True})
                        if opened is not None and opened["status"] == "open":
                            # Un minuto explícito sin operaciones veta nuevas entradas,
                            # pero no es una pérdida de datos ni un precio de salida.
                            exit_price = row["price"] if bars and bars[-1].volume > 0 else None
                            exit_quality = "complete" if exit_price is not None else "incomplete"
                            exit_note = (
                                "; sin precio negociado en la última vela"
                                if exit_price is None
                                else ""
                            )
                            exit_reasons = [
                                r
                                for r in row["exit_reasons"]
                                if not r.startswith("Nota inferior") and r != _NO_TRADES_REASON
                            ]
                            if (
                                row["score"] is not None
                                and row["score"] <= opened["config"]["exit_score"]
                            ):
                                finish(
                                    opened,
                                    "score",
                                    self.clock.now(),
                                    "Puntuación de salida alcanzada" + exit_note,
                                    price=exit_price,
                                    quality=exit_quality,
                                )
                            elif exit_reasons:
                                finish(
                                    opened,
                                    "filters",
                                    self.clock.now(),
                                    "; ".join(exit_reasons) + exit_note,
                                    price=exit_price,
                                    quality=exit_quality,
                                )
                            elif self.clock.now() >= timestamp(opened["created_at"]) + timedelta(
                                minutes=opened["config"]["lifetime_minutes"]
                            ):
                                finish(
                                    opened,
                                    "expired",
                                    timestamp(opened["created_at"])
                                    + timedelta(minutes=opened["config"]["lifetime_minutes"]),
                                    "Caducidad sin precio exacto en ese instante",
                                    quality="incomplete",
                                )
                            changed[opened["id"]] = opened
                        if opened is not None:
                            row["opportunity_id"] = opened["id"]
                            if opened["status"] == "open":
                                row["state"] = "open"
                            else:
                                state["armed"] = False
                        elif row["state"] == "eligible" and state["armed"]:
                            p = self._new_opportunity(row, self.clock.now())
                            # Una identidad ya guardada no vuelve a abrirse tras reinicio.
                            if p["id"] not in self._opportunities:
                                self._opportunities[p["id"]] = p
                                changed[p["id"]] = p
                                notices.append(self._notice(p, "start"))
                                row.update(state="open", opportunity_id=p["id"])
                                state["armed"] = False
                        elif (
                            row["score"] is not None
                            and not stale
                            and self._fresh(
                                tickers.get(symbol, {}), self.config.quote_max_age_seconds
                            )
                        ):
                            if row["state"] != "eligible":
                                state["armed"] = True
                        rows.append(row)
                    except Exception as exc:
                        errors.append(f"{symbol}: {type(exc).__name__}")
                        interrupted = self._open_for(symbol)
                        if interrupted is not None:
                            interrupted["tracking_gaps"] = True
                            finish(
                                interrupted,
                                "interrupted",
                                self.clock.now(),
                                "Error de datos durante seguimiento",
                                quality="incomplete",
                            )
                            changed[interrupted["id"]] = interrupted
                        rows.append(
                            {
                                "market": f"revolutx:{symbol}",
                                "bar_ts": None,
                                "observed_at": self.clock.now().isoformat(),
                                "rule_version": self.rule_version,
                                "state": "unavailable",
                                "score": None,
                                "points": {},
                                "max_points": {},
                                "positive": [],
                                "negative": [],
                                "reasons": [errors[-1]],
                                "price": None,
                                "atr_pct": None,
                                "spread_pct": None,
                                "quote_volume": None,
                                "costs": None,
                                "opportunity_id": None,
                            }
                        )
                    self._event()
                # Recuperación acotada de horizontes de monedas que ya salieron de la selección.
                pending: list[dict[str, Any]] = []
                for p in self._opportunities.values():
                    symbol = p["market"].split(":", 1)[1]
                    created = timestamp(p["created_at"])
                    due = any(
                        h["at"] is None and now >= created + timedelta(minutes=HORIZONS[k])
                        for k, h in p["horizons"].items()
                    )
                    cached = self._bars.get(symbol, [])
                    if due and (
                        symbol not in self.selected
                        or not cached
                        or cached[0].open_time > created + timedelta(minutes=1)
                    ):
                        pending.append(p)
                for p in pending[:8]:
                    try:
                        await self._recover_horizons(p)
                        changed[p["id"]] = p
                    except Exception as exc:
                        errors.append(f"Historial {p['market']}: {type(exc).__name__}")
            except Exception as exc:
                errors.append(type(exc).__name__)
                for ident in previous_open:
                    p = self._opportunities[ident]
                    p["tracking_gaps"] = True
                    finish(
                        p,
                        "interrupted",
                        self.clock.now(),
                        "Fuente de datos no disponible",
                        quality="incomplete",
                    )
                    changed[ident] = p
            for ident in previous_open:
                p = self._opportunities[ident]
                if p["status"] != "open":
                    self._states.setdefault(p["market"], {})["armed"] = False
                    notices.append(self._notice(p, "end"))
            self.cycles += 1
            self.last_cycle_at = self.clock.now().isoformat()
            self.last_error = "; ".join(errors) or None
            self.state = "degraded" if errors else "running"
            published_rows = sorted(
                rows, key=lambda r: r["score"] if r["score"] is not None else -1, reverse=True
            )
            self._states["service"] = {
                "cycles": self.cycles,
                "last_cycle_at": self.last_cycle_at,
                "selected": self.selected,
                "excluded": self.excluded,
                "rows": published_rows,
                "config": self.config.model_dump(mode="json"),
                "coverage": self.coverage(),
            }
            await self.repository.commit_cycle(
                observations=[r for r in rows if r["bar_ts"] is not None],
                opportunities=list(changed.values()),
                states=self._states,
                notifications=notices,
            )
            self.rows = published_rows
            if self.simulation is not None:
                await self.simulation.cycle(
                    rows=published_rows,
                    opportunities=list(self._opportunities.values()),
                    books=self._cycle_books,
                    markets=self._markets,
                    analysis_cycle=self.cycles,
                )
            if self.experiments is not None:
                await self.experiments.cycle(
                    rows=published_rows,
                    bars=self._bars,
                    books=self._cycle_books,
                    markets=self._markets,
                    analysis_cycle=self.cycles,
                )
            if self.execution_experiment is not None:
                await self.execution_experiment.cycle(
                    rows=published_rows,
                    bars=self._bars,
                    books=self._cycle_books,
                    markets=self._markets,
                    analysis_cycle=self.cycles,
                )
            if self.timeframe_experiment is not None:
                await self.timeframe_experiment.cycle(
                    rows=published_rows,
                    bars=self._bars,
                    books=self._cycle_books,
                    markets=self._markets,
                    analysis_cycle=self.cycles,
                )
            self._event()
            log.info(
                "analysis_cycle",
                cycle=self.cycles,
                markets=len(rows),
                errors=errors,
                opened=sum(p["status"] == "open" for p in self._opportunities.values()),
            )

    async def run(self, stop: asyncio.Event, *, max_cycles: int | None = None) -> None:
        await self.start()
        count = 0
        try:
            while not stop.is_set():
                started = asyncio.get_running_loop().time()
                await self.cycle()
                count += 1
                if max_cycles is not None and count >= max_cycles:
                    break
                delay = max(
                    0.0,
                    self.config.interval_seconds - (asyncio.get_running_loop().time() - started),
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay)
        finally:
            self.running = False
            self.state = "stopped"
            self._event()

    async def close(self) -> None:
        self.running = False
        self.state = "stopped"
        await self.feed.close()
