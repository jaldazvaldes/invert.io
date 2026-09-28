"""Traduce los eventos del motor a avisos legibles (entrada, stop, objetivo, resultado…)."""

from __future__ import annotations

from decimal import Decimal

from invertio.config.app_config import AppConfig
from invertio.core.bus import EventBus
from invertio.core.events import (
    EngineState,
    EngineStateChanged,
    OrderFilled,
    OrderUpdated,
    RiskDecision,
)
from invertio.core.models import OrderStatus, OrderType, Side, SignalAction
from invertio.notify import Notifier
from invertio.notify.format import (
    currency_symbol,
    html,
    money,
    pct,
    price,
    quantity,
    signed_money,
)
from invertio.portfolio import Portfolio

_EXIT_REASONS = {
    "stop_loss": "stop-loss",
    "take_profit": "objetivo alcanzado",
    "señal de salida": "señal de salida",
    "pánico": "botón de pánico",
}


class EventNotifications:
    def __init__(
        self,
        bus: EventBus,
        notifier: Notifier,
        config: AppConfig,
        portfolio: Portfolio,
        *,
        notify_rejections: bool = False,
    ) -> None:
        self._notifier = notifier
        self._config = config
        self._portfolio = portfolio
        self._notify_rejections = notify_rejections
        self._trades_seen = len(portfolio.closed_trades)  # tras recuperar el historial
        bus.subscribe(RiskDecision, self._on_decision)
        bus.subscribe(OrderFilled, self._on_fill)
        bus.subscribe(OrderUpdated, self._on_order)
        bus.subscribe(EngineStateChanged, self._on_state)

    def _currency(self, venue: str) -> str:
        return self._config.venue(venue).quote_currency

    async def _on_decision(self, event: RiskDecision) -> None:
        signal = event.signal
        if not event.approved:
            if self._notify_rejections:
                await self._notifier.send(
                    f"🚫 Señal descartada · <b>{html(signal.symbol)}</b>\n"
                    f"{html(event.reason)} ({html(signal.strategy_id)})"
                )
            return
        if signal.action is SignalAction.CLOSE or event.order is None:
            return  # el aviso llega al ejecutarse la venta
        order = event.order
        currency = self._currency(signal.venue)
        entry = order.limit_price or Decimal(str(signal.price))
        assert order.stop_loss is not None
        lines = [
            f"📈 <b>Señal de compra</b> · <b>{html(signal.symbol)}</b>",
            f"Entrada ~{price(entry)} {html(currency_symbol(currency))}"
            + (" (orden límite)" if order.type is OrderType.LIMIT else " (a mercado)"),
            f"Stop {price(order.stop_loss)} ({pct(float(order.stop_loss / entry - 1) * 100)})",
        ]
        if order.take_profit is not None:
            gain = float(order.take_profit / entry - 1) * 100
            lines.append(f"Objetivo {price(order.take_profit)} ({pct(gain)})")
        else:
            lines.append("Objetivo: salida por señal de la estrategia")
        notional = order.quantity * entry
        max_loss = order.quantity * (entry - order.stop_loss)
        lines.append(
            f"Tamaño {quantity(order.quantity)} ≈ {money(notional, currency)} · "
            f"pérdida máx. ≈ {money(max_loss, currency)}"
        )
        lines.append(f"<i>{html(signal.strategy_id)}: {html(signal.reason)}</i>")
        await self._notifier.send("\n".join(lines))

    async def _on_fill(self, event: OrderFilled) -> None:
        fill = event.fill
        currency = self._currency(fill.venue)
        if fill.side is Side.BUY:
            position = self._portfolio.position(fill.venue, fill.symbol)
            lines = [
                f"✅ <b>Compra ejecutada</b> · <b>{html(fill.symbol)}</b>",
                f"{quantity(fill.quantity)} a {price(fill.price)} = "
                f"{money(fill.notional, currency)} · comisión {money(fill.fee, currency)}",
            ]
            if position.stop_loss is not None:
                target = price(position.take_profit) if position.take_profit else "por señal"
                lines.append(f"Stop {price(position.stop_loss)} · objetivo {target}")
            await self._notifier.send("\n".join(lines))
            return
        trades = self._portfolio.closed_trades
        if len(trades) > self._trades_seen:
            self._trades_seen = len(trades)
            trade = trades[-1]
            icon = "🟢" if trade.pnl > 0 else "🔴"
            reason = _EXIT_REASONS.get(trade.exit_reason, trade.exit_reason)
            await self._notifier.send(
                f"{icon} <b>Venta ejecutada</b> · <b>{html(fill.symbol)}</b> ({html(reason)})\n"
                f"{quantity(fill.quantity)} a {price(fill.price)} · "
                f"resultado {signed_money(trade.pnl, currency)} ({pct(trade.return_pct)})\n"
                f"Capital {html(fill.venue)}: "
                f"{money(self._portfolio.equity(fill.venue), currency)}"
            )

    async def _on_order(self, event: OrderUpdated) -> None:
        order = event.order
        if order.status is OrderStatus.CANCELED and order.filled_quantity == 0:
            await self._notifier.send(
                f"⌛ Orden de {'compra' if order.request.side is Side.BUY else 'venta'} "
                f"<b>{html(order.request.symbol)}</b> no ejecutada: cancelada"
            )
        elif order.status is OrderStatus.REJECTED:
            await self._notifier.send(
                f"❗ Orden <b>{html(order.request.symbol)}</b> rechazada: "
                f"{html(order.reject_reason or 'sin motivo')}"
            )

    async def _on_state(self, event: EngineStateChanged) -> None:
        icons = {EngineState.RUNNING: "▶️", EngineState.PAUSED: "⏸", EngineState.HALTED: "🛑"}
        names = {
            EngineState.RUNNING: "en marcha",
            EngineState.PAUSED: "en pausa (no abre posiciones nuevas)",
            EngineState.HALTED: "DETENIDO",
        }
        await self._notifier.send(
            f"{icons[event.state]} Motor {names[event.state]}: {html(event.reason)}"
        )
