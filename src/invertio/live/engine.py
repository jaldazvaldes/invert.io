"""Motor en vivo (fase 2: paper trading con precios reales y bróker simulado).

Usa las mismas piezas que el backtest: estrategia, gestor de riesgo, portfolio y SimBroker.
Cambian el reloj (hora real), los datos (sondeo tras cada cierre de vela) y que todo queda
grabado en SQLite y se avisa por Telegram.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import groupby
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from invertio.config.app_config import AppConfig, VenueConfig
from invertio.config.live_config import LiveConfig
from invertio.core.bus import EventBus
from invertio.core.clock import Clock
from invertio.core.events import BarClosed
from invertio.core.models import AssetClass, Bar, TradingMode
from invertio.core.timeframes import floor_to_timeframe, timeframe_delta
from invertio.data.sessions import in_us_regular_session
from invertio.data.store import BarStore
from invertio.execution import OrderRouter
from invertio.execution.sim import SimBroker
from invertio.live.controller import EngineController
from invertio.live.feeds import LiveFeed, SpreadBook
from invertio.notify import Notifier
from invertio.notify.format import html, money
from invertio.notify.messages import EventNotifications
from invertio.persistence.recorder import Recorder, last_equity_before, load_state
from invertio.portfolio import Portfolio
from invertio.risk import RiskManager
from invertio.strategies import Strategy
from invertio.strategies.runner import StrategyRunner

log = structlog.get_logger(__name__)

POLL_GRACE = timedelta(seconds=4)  # margen tras el cierre para que el exchange publique la vela


@dataclass(slots=True)
class MarketRuntime:
    venue: VenueConfig
    symbol: str
    strategy: Strategy[Any]
    feed: LiveFeed
    last_open: datetime | None = None
    stale: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.venue.id, self.symbol)

    @property
    def label(self) -> str:
        return f"{self.venue.id}:{self.symbol}"


class LiveEngine:
    def __init__(
        self,
        *,
        config: AppConfig,
        live: LiveConfig,
        mode: TradingMode,
        timeframe: str,
        markets: list[MarketRuntime],
        sessions: async_sessionmaker[AsyncSession],
        notifier: Notifier,
        clock: Clock,
        store: BarStore | None = None,
    ) -> None:
        if mode is not TradingMode.PAPER:
            raise NotImplementedError(f"El modo {mode.value} llega en la fase 4")
        if not markets:
            raise ValueError("No hay mercados que vigilar")
        self.config = config
        self.live = live
        self.mode = mode
        self.timeframe = timeframe
        self.tf = timeframe_delta(timeframe)
        self.markets = markets
        self.clock = clock
        self.store = store
        self._sessions = sessions
        self._notifier = notifier
        self._tz = ZoneInfo(config.risk.day_timezone)

        venues = {m.venue.id: m.venue for m in markets}
        self.bus = EventBus(strict=False)
        # El grabador se suscribe el primero: el bus reparte en orden de suscripción y así
        # cada señal se graba antes que la orden que provoca (clave foránea).
        self.recorder = Recorder(self.bus, clock, sessions, mode)
        self.portfolio = Portfolio(
            self.bus,
            {v: live.initial_cash[v] for v in venues},
            {v: cfg.quote_currency for v, cfg in venues.items()},
        )
        self.spreads = SpreadBook(
            clock,
            fallback_pct={
                m.venue.id: None if m.feed.has_live_spread else m.venue.simulation.spread_pct
                for m in markets
            },
            max_age=2 * self.tf,
        )
        self.risk = RiskManager(
            self.bus, clock, config, self.portfolio, spread_pct=self.spreads.get
        )
        self.brokers = {
            v: SimBroker(self.bus, clock, cfg, self.portfolio) for v, cfg in venues.items()
        }
        OrderRouter(self.bus, dict(self.brokers))
        self.runner = StrategyRunner(self.bus, self.portfolio, {m.key: m.strategy for m in markets})
        self.controller = EngineController(
            self.bus,
            clock,
            config,
            mode,
            self.portfolio,
            self.risk,
            self.brokers,
            self.runner,
        )
        self.notifications: EventNotifications | None = None

    # --- ciclo de vida -----------------------------------------------------------------

    async def start(self) -> None:
        self.recorder.start()
        now = self.clock.now()
        state = await load_state(self._sessions, self.mode, now)
        self.portfolio.replay(state.requests, state.fills)
        for venue_id, broker in self.brokers.items():
            for position in self.portfolio.open_positions(venue_id):
                if position.stop_loss is not None:
                    broker.restore_bracket(
                        position.symbol,
                        self.portfolio.entry_strategy(venue_id, position.symbol),
                        position.stop_loss,
                        position.take_profit,
                    )
        midnight = datetime.combine(now.astimezone(self._tz).date(), datetime.min.time(), self._tz)
        self.risk.restore_equity_marks(
            await last_equity_before(self._sessions, self.mode, midnight.astimezone(UTC))
        )
        # Los avisos se conectan después de recuperar el historial: no se repiten.
        self.notifications = EventNotifications(
            self.bus,
            self._notifier,
            self.config,
            self.portfolio,
            notify_rejections=self.live.notify_rejections,
        )
        await self._warm_up(state.last_snapshot)
        await self._notifier.send(self._startup_message(state.canceled_on_restart))
        self.recorder.audit(now, "info", "engine_started", {"mode": self.mode.value})

    async def run(self, stop: asyncio.Event) -> None:
        await self.start()
        try:
            while not stop.is_set():
                delay = (self._next_poll(self.clock.now()) - self.clock.now()).total_seconds()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=max(delay, 0))
                if stop.is_set():
                    break
                try:
                    await self.tick()
                except Exception as exc:
                    log.exception("error en el ciclo del motor")
                    await self._notifier.send(f"❗ Error en el motor: {html(str(exc))[:300]}")
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        self.recorder.audit(self.clock.now(), "info", "engine_stopped", {})
        await self._notifier.send("⏹ invert.io detenido. Las posiciones simuladas se conservan.")
        await self.recorder.stop()
        for market in self.markets:
            try:
                await market.feed.close()
            except Exception:
                log.exception("error cerrando el feed", market=market.label)

    # --- ciclo por vela ----------------------------------------------------------------

    async def tick(self) -> None:
        now = self.clock.now()
        new_bars: list[Bar] = []
        polled: dict[int, tuple[LiveFeed, list[MarketRuntime]]] = {}
        for market in self.markets:
            if not self._market_open(market, now):
                continue
            since = market.last_open + self.tf if market.last_open else now - self._lookback(market)
            try:
                bars = await market.feed.closed_bars(market.symbol, self.timeframe, since, now)
            except Exception as exc:
                log.warning("fallo al pedir datos", market=market.label, error=str(exc))
                continue
            polled.setdefault(id(market.feed), (market.feed, []))[1].append(market)
            new_bars.extend(bars)
            self._store(market, bars)
        await self._update_spreads(list(polled.values()))
        await self._process(new_bars, now, catch_up_after=None)
        await self._check_staleness(now)

    async def _update_spreads(self, groups: list[tuple[LiveFeed, list[MarketRuntime]]]) -> None:
        """Un único sondeo de spreads por feed (todos sus mercados en una petición)."""
        for feed, markets in groups:
            if not feed.has_live_spread:
                continue
            try:
                spreads = await feed.spreads([m.symbol for m in markets])
            except Exception as exc:
                log.warning("fallo al pedir spreads", venue=feed.venue, error=str(exc))
                continue
            for market in markets:
                if market.symbol in spreads:
                    self.spreads.update(market.venue.id, market.symbol, spreads[market.symbol])

    async def _process(
        self, bars: list[Bar], now: datetime, *, catch_up_after: dict[str, datetime] | None
    ) -> None:
        """Procesa velas en orden cronológico.

        `catch_up_after=None` (vivo): todas pasan por el bróker y solo las recién cerradas
        pueden generar señales. En el calentamiento se pasa un dict: por el bróker solo pasan
        las velas posteriores a la última equity anotada (para aplicar los stops que se habrían
        tocado mientras la app estaba parada) y ninguna genera señales.
        """
        by_key = {m.key: m for m in self.markets}
        live_window = max(self.tf / 2, timedelta(seconds=60))
        for _, group in groupby(sorted(bars, key=lambda b: b.open_time), key=lambda b: b.open_time):
            group_bars = list(group)
            traded: set[str] = set()  # venues cuyas velas pasan por el bróker (y se anotan)
            for bar in group_bars:
                resume = catch_up_after.get(bar.venue) if catch_up_after is not None else None
                if catch_up_after is None or (resume is not None and bar.close_time > resume):
                    await self.brokers[bar.venue].process_bar(bar)
                    traded.add(bar.venue)
            for bar in group_bars:
                live = catch_up_after is None and now - bar.close_time <= live_window
                await self.bus.publish(BarClosed(bar, live=live))
                market = by_key[(bar.venue, bar.symbol)]
                market.last_open = bar.open_time
                self.controller.last_bar[market.key] = bar.close_time
                if market.stale:
                    market.stale = False
                    await self._notifier.send(f"✅ Datos de {html(market.label)} recuperados")
            # Las velas de puro calentamiento no se anotan: son historia anterior al arranque.
            for venue_id in traded:
                self._snapshot(group_bars[0].close_time, venue_id)

    def _snapshot(self, ts: datetime, venue_id: str) -> None:
        self.recorder.snapshot_equity(
            ts,
            venue_id,
            self.config.venue(venue_id).quote_currency,
            self.portfolio.equity(venue_id),
            self.portfolio.cash(venue_id),
        )

    async def _warm_up(self, last_snapshot: dict[str, datetime]) -> None:
        now = self.clock.now()
        bars: list[Bar] = []
        for market in self.markets:
            try:
                history = await market.feed.closed_bars(
                    market.symbol, self.timeframe, now - self._lookback(market), now
                )
            except Exception as exc:
                log.warning("no se pudo calentar", market=market.label, error=str(exc))
                await self._notifier.send(
                    f"⚠️ No se pudieron cargar datos de {html(market.label)}: {html(str(exc))[:200]}"
                )
                continue
            if len(history) < market.strategy.warmup_bars:
                log.warning(
                    "calentamiento incompleto",
                    market=market.label,
                    velas=len(history),
                    necesarias=market.strategy.warmup_bars,
                )
            bars.extend(history)
            self._store(market, history)
        await self._process(bars, now, catch_up_after=last_snapshot)
        # Una marca de equity al arrancar, con la última vela conocida de cada venue.
        for venue_id in self.brokers:
            closes = [b.close_time for b in bars if b.venue == venue_id]
            if closes and max(closes) > last_snapshot.get(
                venue_id, datetime.min.replace(tzinfo=UTC)
            ):
                self._snapshot(max(closes), venue_id)

    async def _check_staleness(self, now: datetime) -> None:
        limit = self.live.stale_after_bars * self.tf
        for market in self.markets:
            if market.stale or not self._market_open(market, now, strict=True):
                continue
            last_close = market.last_open + self.tf if market.last_open else None
            if last_close is None or now - last_close > limit:
                market.stale = True
                since = f"desde {last_close:%H:%M} UTC" if last_close else "desde el arranque"
                await self._notifier.send(
                    f"⚠️ Sin datos nuevos de {html(market.label)} {since}: no se abrirán "
                    "posiciones en ese mercado hasta que vuelvan"
                )

    # --- utilidades --------------------------------------------------------------------

    def _next_poll(self, now: datetime) -> datetime:
        return floor_to_timeframe(now, self.timeframe) + self.tf + POLL_GRACE

    def _market_open(self, market: MarketRuntime, now: datetime, *, strict: bool = False) -> bool:
        """Cripto: siempre. Acciones: durante la sesión regular de Nueva York (y justo al
        cerrar, para recoger la última vela). Con `strict`, solo con la sesión en curso."""
        if market.venue.asset_class is not AssetClass.STOCK:
            return True
        if strict:
            return in_us_regular_session(now - self.tf) and in_us_regular_session(now)
        return in_us_regular_session(now - self.tf)

    def _lookback(self, market: MarketRuntime) -> timedelta:
        bars = market.strategy.warmup_bars + 20
        if market.venue.asset_class is AssetClass.STOCK:
            trading_days = math.ceil(bars * self.tf.total_seconds() / (6.5 * 3600))
            return timedelta(days=math.ceil(trading_days * 7 / 5) + 4)
        return bars * self.tf

    def _store(self, market: MarketRuntime, bars: list[Bar]) -> None:
        if self.store is not None and bars:
            try:
                self.store.write(market.venue.id, market.symbol, self.timeframe, bars)
            except Exception:
                log.exception("no se pudieron guardar las velas", market=market.label)

    def _startup_message(self, canceled: int) -> str:
        lines = [
            f"🟢 <b>invert.io</b> en marcha · modo <b>{self.mode.value}</b> · "
            f"velas {self.timeframe}"
        ]
        by_strategy: dict[str, list[str]] = {}
        for market in self.markets:
            by_strategy.setdefault(market.strategy.id, []).append(market.symbol)
        for strategy_id, symbols in by_strategy.items():
            shown = ", ".join(symbols[:6]) + (
                f" y {len(symbols) - 6} más" if len(symbols) > 6 else ""
            )
            lines.append(f"• {html(strategy_id)}: {len(symbols)} mercados ({html(shown)})")
        for venue in self.controller.status():
            lines.append(f"Capital {html(venue.venue)}: {money(venue.equity, venue.currency)}")
            for p in venue.positions:
                lines.append(f"  posición abierta: {html(p.symbol)}")
        if canceled:
            lines.append(f"{canceled} órdenes pendientes canceladas por el reinicio")
        return "\n".join(lines)
