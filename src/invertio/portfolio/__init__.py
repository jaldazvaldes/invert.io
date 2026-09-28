"""Portfolio: efectivo, posiciones, equity y operaciones cerradas, por venue."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from invertio.core.bus import EventBus
from invertio.core.events import BarClosed, OrderFilled, OrderUpdated
from invertio.core.models import ZERO, Fill, OrderRequest, Position, Side, to_decimal

type Key = tuple[str, str]  # (venue, símbolo)


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """Operación completa (entrada → salida). `pnl` es neto de comisiones de entrada y salida."""

    venue: str
    symbol: str
    strategy_id: str
    entry_time: datetime
    exit_time: datetime
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    pnl: Decimal
    fees: Decimal
    exit_reason: str

    @property
    def return_pct(self) -> float:
        cost = self.entry_price * self.quantity
        return float(self.pnl / cost * 100) if cost else 0.0

    @property
    def is_win(self) -> bool:
        return self.pnl > 0


@dataclass(slots=True)
class _OpenTrade:
    strategy_id: str
    entry_time: datetime
    realized_at_open: Decimal
    fees_at_open: Decimal
    max_quantity: Decimal
    entry_price: Decimal


class Portfolio:
    def __init__(self, bus: EventBus, cash: dict[str, Decimal], quote: dict[str, str]) -> None:
        """`cash` y `quote`: efectivo inicial y moneda de cotización de cada venue."""
        self._cash = dict(cash)
        self.initial_cash = dict(cash)
        self._quote = dict(quote)
        self.positions: dict[Key, Position] = {}
        self.closed_trades: list[ClosedTrade] = []
        self._last_price: dict[Key, Decimal] = {}
        self._requests: dict[str, OrderRequest] = {}
        self._open_trades: dict[Key, _OpenTrade] = {}
        bus.subscribe(OrderUpdated, self._on_order)
        bus.subscribe(OrderFilled, self._on_fill)
        bus.subscribe(BarClosed, self._on_bar)

    # --- consultas ---------------------------------------------------------------------

    def cash(self, venue: str) -> Decimal:
        return self._cash[venue]

    def position(self, venue: str, symbol: str) -> Position:
        return self.positions.get((venue, symbol)) or Position(venue, symbol)

    def open_positions(self, venue: str | None = None) -> list[Position]:
        return [
            p for p in self.positions.values() if p.is_open and (venue is None or p.venue == venue)
        ]

    def last_price(self, venue: str, symbol: str) -> Decimal | None:
        return self._last_price.get((venue, symbol))

    def entry_strategy(self, venue: str, symbol: str) -> str:
        """Estrategia que abrió la posición actual del mercado ("" si no hay)."""
        trade = self._open_trades.get((venue, symbol))
        return trade.strategy_id if trade else ""

    def equity(self, venue: str) -> Decimal:
        value = self._cash[venue]
        for position in self.open_positions(venue):
            price = self._last_price.get((venue, position.symbol), position.avg_price)
            value += position.quantity * price
        return value

    def replay(self, requests: dict[str, OrderRequest], fills: list[Fill]) -> None:
        """Reconstruye el estado a partir del historial (tras un reinicio), sin emitir eventos."""
        self._requests.update(requests)
        for fill in fills:
            self._on_fill(OrderFilled(fill))

    # --- eventos -----------------------------------------------------------------------

    def _on_order(self, event: OrderUpdated) -> None:
        self._requests.setdefault(event.order.client_order_id, event.order.request)

    def _on_bar(self, event: BarClosed) -> None:
        bar = event.bar
        self._last_price[(bar.venue, bar.symbol)] = to_decimal(bar.close)

    def _on_fill(self, event: OrderFilled) -> None:
        fill = event.fill
        key = (fill.venue, fill.symbol)
        fee = self._fee_in_quote(fill)
        position = self.positions.setdefault(key, Position(fill.venue, fill.symbol))
        request = self._requests.get(fill.client_order_id)

        if fill.side is Side.BUY:
            self._cash[fill.venue] -= fill.notional + fee
            if not position.is_open:
                self._open_trades[key] = _OpenTrade(
                    strategy_id=request.strategy_id if request else "",
                    entry_time=fill.ts,
                    realized_at_open=position.realized_pnl,
                    fees_at_open=position.fees_paid,
                    max_quantity=ZERO,
                    entry_price=ZERO,
                )
            position.apply_fill(fill, fee)
            trade = self._open_trades[key]
            trade.max_quantity = position.quantity
            trade.entry_price = position.avg_price
            if request is not None:
                position.stop_loss = request.stop_loss
                position.take_profit = request.take_profit
        else:
            self._cash[fill.venue] += fill.notional - fee
            position.apply_fill(fill, fee)
            if not position.is_open:
                self._close_trade(key, position, fill, request)
        self._last_price.setdefault(key, fill.price)

    def _close_trade(
        self, key: Key, position: Position, fill: Fill, request: OrderRequest | None
    ) -> None:
        trade = self._open_trades.pop(key)
        self.closed_trades.append(
            ClosedTrade(
                venue=fill.venue,
                symbol=fill.symbol,
                strategy_id=trade.strategy_id,
                entry_time=trade.entry_time,
                exit_time=fill.ts,
                quantity=trade.max_quantity,
                entry_price=trade.entry_price,
                exit_price=fill.price,
                pnl=position.realized_pnl - trade.realized_at_open,
                fees=position.fees_paid - trade.fees_at_open,
                exit_reason=request.reason if request else "",
            )
        )

    def _fee_in_quote(self, fill: Fill) -> Decimal:
        quote = self._quote[fill.venue]
        if fill.fee_currency == quote:
            return fill.fee
        base = fill.symbol.split("/")[0]
        if fill.fee_currency == base:
            return fill.fee * fill.price
        raise ValueError(f"Comisión en moneda inesperada {fill.fee_currency} para {fill.symbol}")
