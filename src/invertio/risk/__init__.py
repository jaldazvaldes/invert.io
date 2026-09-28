"""Gestor de riesgo: convierte señales (float) en órdenes (Decimal) o las rechaza con un motivo.

Toda señal pasa por aquí. Las compras se dimensionan por riesgo (pérdida máxima si salta el
stop, con costes incluidos) y se limitan por tamaño máximo, efectivo disponible y mínimos del
mercado. Los cierres siempre se permiten: reducir riesgo nunca se bloquea.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from invertio.config.app_config import AppConfig, VenueConfig
from invertio.core.bus import EventBus
from invertio.core.clock import Clock
from invertio.core.events import (
    BarClosed,
    EngineState,
    EngineStateChanged,
    OrderUpdated,
    RiskDecision,
    SignalEmitted,
)
from invertio.core.models import (
    OrderRequest,
    OrderType,
    Side,
    Signal,
    SignalAction,
    round_down,
    to_decimal,
)
from invertio.portfolio import Portfolio

HUNDRED = Decimal(100)

type SpreadProvider = Callable[[str, str], float | None]  # (venue, símbolo) → spread en %


class RiskManager:
    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        config: AppConfig,
        portfolio: Portfolio,
        *,
        spread_pct: SpreadProvider | None = None,
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._config = config
        self._risk = config.risk
        self._portfolio = portfolio
        self._spread_pct = spread_pct
        self._tz = ZoneInfo(self._risk.day_timezone)
        self.state = EngineState.RUNNING
        self._pending_buys: dict[str, tuple[str, str]] = {}  # client_order_id → (venue, símbolo)
        self._pending_sells: dict[str, tuple[str, str]] = {}
        self._order_times: deque[datetime] = deque()
        self._day: date | None = None
        self._day_start_equity: dict[str, Decimal] = {}
        # Última equity anotada de cada venue (al cierre de cada vela). El día nuevo arranca
        # con la última marca del día anterior, así cuentan las pérdidas de sus primeros minutos.
        self._equity_marks: dict[str, Decimal] = {
            venue: portfolio.equity(venue) for venue in portfolio.initial_cash
        }
        self._blocked_today: dict[str, str] = {}  # venue → motivo
        bus.subscribe(BarClosed, self._on_bar)
        bus.subscribe(SignalEmitted, self._on_signal)
        bus.subscribe(OrderUpdated, self._on_order)
        bus.subscribe(EngineStateChanged, self._on_state)

    # --- eventos -----------------------------------------------------------------------

    def _on_bar(self, event: BarClosed) -> None:
        self._roll_day(self._clock.now())
        venue = event.bar.venue
        if venue in self._equity_marks:
            self._equity_marks[venue] = self._portfolio.equity(venue)

    def _on_state(self, event: EngineStateChanged) -> None:
        self.state = event.state

    def restore_equity_marks(self, marks: dict[str, Decimal]) -> None:
        """Tras un reinicio: última equity conocida del día anterior, para el límite diario."""
        self._equity_marks.update(marks)
        self._day = None

    async def _on_signal(self, event: SignalEmitted) -> None:
        await self._bus.publish(self.evaluate(event.signal))

    def _on_order(self, event: OrderUpdated) -> None:
        order = event.order
        pending = self._pending_buys if order.request.side is Side.BUY else self._pending_sells
        if order.status.is_terminal:
            pending.pop(order.client_order_id, None)
        else:
            pending[order.client_order_id] = (order.request.venue, order.request.symbol)

    # --- decisión ----------------------------------------------------------------------

    def evaluate(self, signal: Signal) -> RiskDecision:
        now = self._clock.now()
        self._roll_day(now)
        if signal.action is SignalAction.CLOSE:
            return self._evaluate_close(signal)
        return self._evaluate_buy(signal, now)

    def _evaluate_close(self, signal: Signal) -> RiskDecision:
        position = self._portfolio.position(signal.venue, signal.symbol)
        if not position.is_open:
            return self._reject(signal, "no hay posición abierta que cerrar")
        if (signal.venue, signal.symbol) in self._pending_sells.values():
            return self._reject(signal, "ya hay una orden de cierre en curso")
        order = OrderRequest(
            venue=signal.venue,
            symbol=signal.symbol,
            side=Side.SELL,
            type=OrderType.MARKET,
            quantity=position.quantity,
            strategy_id=signal.strategy_id,
            signal_id=signal.id,
            reason="señal de salida",
        )
        return RiskDecision(signal, approved=True, reason="cierre aprobado", order=order)

    def _evaluate_buy(self, signal: Signal, now: datetime) -> RiskDecision:
        if self.state is not EngineState.RUNNING:
            return self._reject(signal, f"motor en estado {self.state.value}: no abre posiciones")
        try:
            venue = self._config.venue(signal.venue)
        except KeyError:
            return self._reject(signal, f"venue desconocido {signal.venue}")
        if not venue.enabled or signal.symbol not in venue.symbols:
            return self._reject(signal, "símbolo fuera de la lista blanca")
        if signal.venue in self._blocked_today:
            return self._reject(signal, self._blocked_today[signal.venue])

        equity = self._portfolio.equity(venue.id)
        start_equity = self._day_start_equity.get(venue.id, equity)
        max_loss = start_equity * to_decimal(self._risk.max_daily_loss_pct) / HUNDRED
        if equity <= start_equity - max_loss:
            return self._block_day(signal, "límite de pérdida diaria alcanzado")
        if self._losing_streak(venue.id, now) >= self._risk.max_consecutive_losses:
            return self._block_day(signal, "demasiadas pérdidas seguidas hoy")

        key = (signal.venue, signal.symbol)
        if self._portfolio.position(*key).is_open or key in self._pending_buys.values():
            return self._reject(signal, "ya hay posición u orden de entrada en este símbolo")
        exposures = len(self._portfolio.open_positions(venue.id)) + sum(
            1 for pending_venue, _ in self._pending_buys.values() if pending_venue == venue.id
        )
        if exposures >= self._risk.max_open_positions:
            return self._reject(signal, "máximo de posiciones abiertas alcanzado")

        while self._order_times and now - self._order_times[0] >= timedelta(hours=1):
            self._order_times.popleft()
        if len(self._order_times) >= self._risk.max_orders_per_hour:
            return self._reject(signal, "máximo de órdenes por hora alcanzado")

        if self._spread_pct is not None:
            spread = self._spread_pct(signal.venue, signal.symbol)
            if spread is None:
                return self._reject(signal, "sin datos de spread: no se entra a ciegas")
            if spread > self._risk.max_spread_pct:
                return self._reject(signal, f"spread demasiado amplio ({spread:.3f} %)")

        order = self._size_buy(signal, venue, equity)
        if isinstance(order, str):
            return self._reject(signal, order)
        self._order_times.append(now)
        return RiskDecision(signal, approved=True, reason="entrada aprobada", order=order)

    def _size_buy(self, signal: Signal, venue: VenueConfig, equity: Decimal) -> OrderRequest | str:
        instrument = venue.instrument_for(signal.symbol)
        price = to_decimal(signal.price)
        assert signal.stop_loss is not None  # garantizado por Signal
        stop = round_down(to_decimal(signal.stop_loss), instrument.price_step)
        if stop <= 0 or stop >= price:
            return "stop-loss no válido tras redondear"

        fees, sim = venue.fees, venue.simulation
        # Coste de ida y vuelta estimado (comisión taker, divisa, spread y deslizamiento).
        round_trip_pct = to_decimal(
            2 * (fees.taker_pct + fees.fx_pct + sim.slippage_pct) + sim.spread_pct
        )
        risk_per_unit = (price - stop) + price * round_trip_pct / HUNDRED
        risk_budget = equity * to_decimal(self._risk.max_risk_per_trade_pct) / HUNDRED
        position_cap = equity * to_decimal(self._risk.max_position_pct) / HUNDRED
        entry_cost_pct = (
            to_decimal(fees.taker_pct + fees.fx_pct + sim.slippage_pct)
            + to_decimal(sim.spread_pct) / 2
        )
        cash_available = self._portfolio.cash(venue.id)
        quantity = min(
            risk_budget / risk_per_unit,
            position_cap / price,
            cash_available / (price * (1 + entry_cost_pct / HUNDRED)),
        )
        quantity = round_down(quantity, instrument.amount_step)
        if quantity <= 0 or quantity < instrument.min_amount:
            return "tamaño por debajo del mínimo del mercado"
        if quantity * price < instrument.min_notional:
            return (
                f"importe por debajo del mínimo ({instrument.min_notional} {venue.quote_currency})"
            )

        take_profit = (
            round_down(to_decimal(signal.take_profit), instrument.price_step)
            if signal.take_profit is not None
            else None
        )
        use_post_only = self._risk.prefer_post_only and venue.supports_post_only
        return OrderRequest(
            venue=venue.id,
            symbol=signal.symbol,
            side=Side.BUY,
            type=OrderType.LIMIT if use_post_only else OrderType.MARKET,
            quantity=quantity,
            limit_price=round_down(price, instrument.price_step) if use_post_only else None,
            post_only=use_post_only,
            stop_loss=stop,
            take_profit=take_profit,
            strategy_id=signal.strategy_id,
            signal_id=signal.id,
            reason="entrada",
        )

    # --- utilidades --------------------------------------------------------------------

    def _roll_day(self, now: datetime) -> None:
        today = now.astimezone(self._tz).date()
        if today != self._day:
            self._day = today
            self._blocked_today.clear()
            self._day_start_equity = dict(self._equity_marks)

    def _losing_streak(self, venue: str, now: datetime) -> int:
        """Pérdidas seguidas hoy (operaciones cerradas en el día local actual)."""
        today = now.astimezone(self._tz).date()
        streak = 0
        for trade in reversed(self._portfolio.closed_trades):
            if trade.venue != venue:
                continue
            if trade.exit_time.astimezone(self._tz).date() != today or trade.is_win:
                break
            streak += 1
        return streak

    def _block_day(self, signal: Signal, reason: str) -> RiskDecision:
        self._blocked_today[signal.venue] = reason
        return self._reject(signal, reason)

    @staticmethod
    def _reject(signal: Signal, reason: str) -> RiskDecision:
        return RiskDecision(signal, approved=False, reason=reason)
