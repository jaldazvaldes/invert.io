"""Bróker simulado para backtest y paper trading.

Reglas de ejecución (conservadoras a propósito):
- Una orden enviada al cierre de una vela se ejecuta con la vela siguiente, nunca antes. En vivo
  la orden nace unos segundos después de ese cierre: si nace dentro de `OPEN_GRACE` tras la
  apertura de una vela, se ejecuta con esa vela; si nace más tarde, espera a la siguiente.
- A mercado: al precio de apertura, pagando medio spread, deslizamiento y comisión taker.
- Límite de compra post-only: se ejecuta a su precio (comisión maker) solo si el mercado cotiza
  por debajo. Si no se ejecuta en `limit_ttl_bars` velas, se cancela.
- Stop-loss: si la vela abre por debajo del stop (hueco), se sale a la apertura; si lo toca,
  al precio del stop. Siempre con deslizamiento y comisión taker.
- Take-profit: orden límite de venta a su precio (comisión maker).
- Si en la misma vela se tocan el stop y el take-profit, se asume el stop (lo peor).
- `flatten` (botón de pánico) vende al momento al último precio conocido, como una orden a
  mercado.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal

from invertio.config.app_config import VenueConfig
from invertio.core.bus import EventBus
from invertio.core.clock import Clock
from invertio.core.events import OrderFilled, OrderUpdated
from invertio.core.models import (
    ZERO,
    Bar,
    Fill,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    to_decimal,
)
from invertio.portfolio import Portfolio

HUNDRED = Decimal(100)
OPEN_GRACE = timedelta(seconds=45)


@dataclass(slots=True)
class _Pending:
    order: Order
    bars_waited: int = 0


@dataclass(slots=True)
class _Bracket:
    strategy_id: str
    stop_loss: Decimal
    take_profit: Decimal | None


class SimBroker:
    def __init__(
        self, bus: EventBus, clock: Clock, venue: VenueConfig, portfolio: Portfolio
    ) -> None:
        self.venue = venue
        self._bus = bus
        self._clock = clock
        self._portfolio = portfolio
        fees, sim = venue.fees, venue.simulation
        self._taker = to_decimal(fees.taker_pct + fees.fx_pct) / HUNDRED
        self._maker = to_decimal(fees.maker_pct + fees.fx_pct) / HUNDRED
        self._market_impact = (
            to_decimal(sim.spread_pct) / 2 + to_decimal(sim.slippage_pct)
        ) / HUNDRED
        self._stop_slippage = to_decimal(sim.slippage_pct) / HUNDRED
        self._ttl = sim.limit_ttl_bars
        self._pending: dict[str, _Pending] = {}
        self._brackets: dict[str, _Bracket] = {}  # símbolo → stop / take-profit activos
        self.orders: dict[str, Order] = {}

    # --- API de bróker -----------------------------------------------------------------

    async def submit(self, request: OrderRequest) -> Order:
        if request.venue != self.venue.id:
            raise ValueError(f"Orden para {request.venue} enviada al simulador de {self.venue.id}")
        now = self._clock.now()
        order = Order(request, created_at=now)
        self.orders[order.client_order_id] = order
        order.transition(OrderStatus.SUBMITTED, now)
        self._pending[order.client_order_id] = _Pending(order)
        await self._publish(order)
        return order

    async def cancel(self, client_order_id: str) -> None:
        pending = self._pending.pop(client_order_id, None)
        if pending is not None:
            pending.order.transition(OrderStatus.CANCELED, self._clock.now())
            await self._publish(pending.order)

    def open_orders(self) -> list[Order]:
        return [p.order for p in self._pending.values()]

    async def cancel_all(self) -> None:
        for client_order_id in list(self._pending):
            await self.cancel(client_order_id)

    async def flatten(self, symbol: str, reason: str) -> None:
        """Cierra la posición ya, al último precio conocido (con spread, deslizamiento y taker)."""
        position = self._portfolio.position(self.venue.id, symbol)
        last = self._portfolio.last_price(self.venue.id, symbol)
        if not position.is_open or last is None:
            return
        now = self._clock.now()
        order = await self._new_exit_order(symbol, position.quantity, reason, now)
        await self._fill(order, last * (1 - self._market_impact), self._taker, now)

    def restore_bracket(
        self, symbol: str, strategy_id: str, stop_loss: Decimal, take_profit: Decimal | None
    ) -> None:
        """Reactiva el stop/take-profit de una posición recuperada tras un reinicio."""
        self._brackets[symbol] = _Bracket(strategy_id, stop_loss, take_profit)

    # --- simulación --------------------------------------------------------------------

    async def process_bar(self, bar: Bar) -> None:
        """Ejecuta contra `bar` las órdenes enviadas antes de su apertura y revisa los stops."""
        if bar.venue != self.venue.id:
            return
        for pending in [p for p in self._pending.values() if p.order.request.symbol == bar.symbol]:
            if pending.order.created_at > bar.open_time + OPEN_GRACE:
                continue  # se envió con esta vela ya en marcha: espera a la siguiente
            await self._try_fill(pending, bar)
        await self._check_brackets(bar)

    async def _try_fill(self, pending: _Pending, bar: Bar) -> None:
        order = pending.order
        request = order.request
        open_price = to_decimal(bar.open)
        at = self._event_time(bar)
        if request.type is OrderType.MARKET:
            direction = 1 if request.side is Side.BUY else -1
            price = open_price * (1 + direction * self._market_impact)
            await self._fill(order, price, self._taker, at)
            return
        assert request.limit_price is not None
        crossed = (
            to_decimal(bar.low) < request.limit_price
            if request.side is Side.BUY
            else to_decimal(bar.high) > request.limit_price
        )
        if crossed:
            await self._fill(order, request.limit_price, self._maker, at)
            return
        pending.bars_waited += 1
        if pending.bars_waited >= self._ttl:
            await self.cancel(order.client_order_id)

    async def _check_brackets(self, bar: Bar) -> None:
        bracket = self._brackets.get(bar.symbol)
        position = self._portfolio.position(self.venue.id, bar.symbol)
        if bracket is None or not position.is_open:
            return
        low, high, open_price = to_decimal(bar.low), to_decimal(bar.high), to_decimal(bar.open)
        if open_price <= bracket.stop_loss:
            price, reason, fee = open_price * (1 - self._stop_slippage), "stop_loss", self._taker
        elif low <= bracket.stop_loss:
            price = bracket.stop_loss * (1 - self._stop_slippage)
            reason, fee = "stop_loss", self._taker
        elif bracket.take_profit is not None and high > bracket.take_profit:
            price, reason, fee = bracket.take_profit, "take_profit", self._maker
        else:
            return
        at = self._event_time(bar)
        order = await self._new_exit_order(bar.symbol, position.quantity, reason, at)
        await self._fill(order, price, fee, at)

    def _event_time(self, bar: Bar) -> datetime:
        """Hora que se anota en una ejecución contra `bar`.

        En backtest el reloj está en la apertura de la vela. En vivo la vela se procesa al
        cerrar, así que se anota la hora real: nunca antes que la orden que se ejecuta.
        """
        return max(bar.open_time, self._clock.now())

    async def _new_exit_order(
        self, symbol: str, quantity: Decimal, reason: str, at: datetime
    ) -> Order:
        bracket = self._brackets.get(symbol)
        request = OrderRequest(
            venue=self.venue.id,
            symbol=symbol,
            side=Side.SELL,
            type=OrderType.MARKET,
            quantity=quantity,
            strategy_id=bracket.strategy_id if bracket else "",
            reason=reason,
        )
        order = Order(request, created_at=at)
        self.orders[order.client_order_id] = order
        order.transition(OrderStatus.SUBMITTED, at)
        await self._publish(order)
        return order

    async def _fill(self, order: Order, price: Decimal, fee_rate: Decimal, at: datetime) -> None:
        request = order.request
        quantity = order.remaining_quantity
        fee = quantity * price * fee_rate
        if request.side is Side.BUY:
            cost = quantity * price + fee
            if cost > self._portfolio.cash(self.venue.id):
                self._pending.pop(order.client_order_id, None)
                order.transition(OrderStatus.REJECTED, at, reason="saldo insuficiente")
                await self._publish(order)
                return
        self._pending.pop(order.client_order_id, None)
        order.apply_fill(quantity, price, at)
        fill = Fill(
            client_order_id=order.client_order_id,
            venue=request.venue,
            symbol=request.symbol,
            side=request.side,
            quantity=quantity,
            price=price,
            fee=fee,
            fee_currency=self.venue.quote_currency,
            ts=at,
        )
        await self._bus.publish(OrderFilled(fill))
        await self._publish(order)
        if request.side is Side.BUY:
            assert request.stop_loss is not None
            self._brackets[request.symbol] = _Bracket(
                request.strategy_id, request.stop_loss, request.take_profit
            )
        elif self._portfolio.position(request.venue, request.symbol).quantity == ZERO:
            self._brackets.pop(request.symbol, None)

    async def _publish(self, order: Order) -> None:
        await self._bus.publish(OrderUpdated(replace(order)))
