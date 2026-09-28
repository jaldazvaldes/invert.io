"""Modelos de dominio.

Convención de tipos:
- Datos de mercado y análisis (velas, indicadores, señales) usan `float`.
- Dinero y cantidades de órdenes, fills y posiciones usan `Decimal`.
El gestor de riesgo es la frontera: convierte señales (float) en órdenes (Decimal)
respetando la precisión de cada mercado.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

from invertio.core.timeframes import timeframe_delta

ZERO = Decimal(0)


def new_id() -> str:
    return uuid.uuid4().hex


def to_decimal(value: float | int | str | Decimal) -> Decimal:
    """Convierte sin arrastrar el ruido binario de los float (0.1 → Decimal('0.1'))."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def round_down(value: Decimal, step: Decimal) -> Decimal:
    """Redondea hacia abajo al múltiplo de `step` (precisión de cantidad o precio)."""
    if step <= 0:
        raise ValueError("El paso de redondeo debe ser positivo")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _require_utc(name: str, ts: datetime) -> None:
    if ts.tzinfo is None or ts.utcoffset() != timedelta(0):
        raise ValueError(f"{name} debe ser un datetime en UTC con zona horaria, no {ts!r}")


class TradingMode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    DEMO = "demo"
    LIVE = "live"


class AssetClass(StrEnum):
    CRYPTO = "crypto"
    STOCK = "stock"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class SignalAction(StrEnum):
    """Solo operamos en largo: BUY abre o amplía una posición y CLOSE la cierra."""

    BUY = "buy"
    CLOSE = "close"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(StrEnum):
    NEW = "new"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATUSES


_TERMINAL_STATUSES = frozenset({OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED})

_ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.NEW: frozenset({OrderStatus.SUBMITTED, OrderStatus.REJECTED, OrderStatus.CANCELED}),
    OrderStatus.SUBMITTED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELED}
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}


class InvalidOrderTransition(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Instrument:
    """Un mercado concreto en un venue, con sus reglas de precisión y mínimos."""

    venue: str
    symbol: str
    asset_class: AssetClass
    base: str
    quote: str
    price_step: Decimal
    amount_step: Decimal
    min_amount: Decimal = ZERO
    min_notional: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class Bar:
    """Vela OHLCV cerrada. `open_time` es el inicio de la vela, en UTC."""

    venue: str
    symbol: str
    timeframe: str
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        _require_utc("open_time", self.open_time)
        if self.high < self.low:
            raise ValueError(f"Vela incoherente (high < low): {self}")

    @property
    def close_time(self) -> datetime:
        return self.open_time + timeframe_delta(self.timeframe)


@dataclass(frozen=True, slots=True)
class Signal:
    """Intención de una estrategia. Aún no es una orden: la valida el gestor de riesgo."""

    strategy_id: str
    venue: str
    symbol: str
    action: SignalAction
    ts: datetime
    price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    reason: str = ""
    id: str = field(default_factory=new_id)

    def __post_init__(self) -> None:
        _require_utc("ts", self.ts)
        if self.price <= 0:
            raise ValueError("El precio de referencia de la señal debe ser positivo")
        if self.action is SignalAction.BUY:
            if self.stop_loss is None:
                raise ValueError("Toda señal de compra necesita stop_loss")
            if not 0 < self.stop_loss < self.price:
                raise ValueError("El stop_loss de una compra debe estar por debajo del precio")
            if self.take_profit is not None and self.take_profit <= self.price:
                raise ValueError("El take_profit de una compra debe estar por encima del precio")


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """Orden ya dimensionada y aprobada por el gestor de riesgo, lista para el bróker.

    Una compra lleva adjuntos su stop-loss (obligatorio) y take-profit (opcional). El bróker
    los activa como órdenes de protección cuando la compra se ejecuta.
    """

    venue: str
    symbol: str
    side: Side
    type: OrderType
    quantity: Decimal
    strategy_id: str
    limit_price: Decimal | None = None
    post_only: bool = False
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    signal_id: str | None = None
    reason: str = ""  # entrada, salida por señal, stop_loss, take_profit, pánico…
    client_order_id: str = field(default_factory=new_id)

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("La cantidad de la orden debe ser positiva")
        if self.type is OrderType.LIMIT and (self.limit_price is None or self.limit_price <= 0):
            raise ValueError("Una orden límite necesita un limit_price positivo")
        if self.type is OrderType.MARKET and (self.limit_price is not None or self.post_only):
            raise ValueError("Una orden a mercado no admite limit_price ni post_only")
        if self.side is Side.BUY:
            if self.stop_loss is None or self.stop_loss <= 0:
                raise ValueError("Toda compra necesita un stop_loss positivo")
            if self.take_profit is not None and self.take_profit <= self.stop_loss:
                raise ValueError("El take_profit debe estar por encima del stop_loss")
        elif self.stop_loss is not None or self.take_profit is not None:
            raise ValueError("Solo las compras llevan stop_loss / take_profit adjuntos")


@dataclass(slots=True)
class Order:
    """Estado vivo de una orden. Las transiciones siguen una máquina de estados estricta."""

    request: OrderRequest
    created_at: datetime
    status: OrderStatus = OrderStatus.NEW
    venue_order_id: str | None = None
    filled_quantity: Decimal = ZERO
    avg_fill_price: Decimal | None = None
    updated_at: datetime | None = None
    reject_reason: str | None = None

    def __post_init__(self) -> None:
        _require_utc("created_at", self.created_at)

    @property
    def client_order_id(self) -> str:
        return self.request.client_order_id

    @property
    def remaining_quantity(self) -> Decimal:
        return self.request.quantity - self.filled_quantity

    def transition(self, new_status: OrderStatus, at: datetime, reason: str | None = None) -> None:
        if new_status not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvalidOrderTransition(
                f"Orden {self.client_order_id}: transición no permitida "
                f"{self.status.value} → {new_status.value}"
            )
        self.status = new_status
        self.updated_at = at
        if new_status is OrderStatus.REJECTED:
            self.reject_reason = reason

    def apply_fill(self, quantity: Decimal, price: Decimal, at: datetime) -> None:
        """Registra una ejecución (total o parcial) y actualiza estado y precio medio."""
        if quantity <= 0 or price <= 0:
            raise ValueError("Cantidad y precio de un fill deben ser positivos")
        if quantity > self.remaining_quantity:
            raise ValueError(
                f"Fill de {quantity} supera lo pendiente ({self.remaining_quantity}) "
                f"en la orden {self.client_order_id}"
            )
        previous_value = self.filled_quantity * (self.avg_fill_price or ZERO)
        self.filled_quantity += quantity
        self.avg_fill_price = (previous_value + quantity * price) / self.filled_quantity
        target = (
            OrderStatus.FILLED if self.remaining_quantity == 0 else OrderStatus.PARTIALLY_FILLED
        )
        self.transition(target, at)


@dataclass(frozen=True, slots=True)
class Fill:
    """Ejecución confirmada. La comisión se expresa en `fee_currency`."""

    client_order_id: str
    venue: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_currency: str
    ts: datetime
    id: str = field(default_factory=new_id)

    def __post_init__(self) -> None:
        _require_utc("ts", self.ts)
        if self.quantity <= 0 or self.price <= 0 or self.fee < 0:
            raise ValueError("Fill con cantidad, precio o comisión no válidos")

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.price


@dataclass(slots=True)
class Position:
    """Posición larga en un instrumento.

    `apply_fill` asume la comisión en la moneda de cotización (quote). El portfolio
    convierte antes las comisiones cobradas en la moneda base.
    """

    venue: str
    symbol: str
    quantity: Decimal = ZERO
    avg_price: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    fees_paid: Decimal = ZERO
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None

    @property
    def is_open(self) -> bool:
        return self.quantity > 0

    def unrealized_pnl(self, last_price: Decimal) -> Decimal:
        return (last_price - self.avg_price) * self.quantity

    def apply_fill(self, fill: Fill, fee_in_quote: Decimal) -> None:
        if (fill.venue, fill.symbol) != (self.venue, self.symbol):
            raise ValueError("El fill no corresponde a esta posición")
        self.fees_paid += fee_in_quote
        self.realized_pnl -= fee_in_quote
        if fill.side is Side.BUY:
            total_cost = self.avg_price * self.quantity + fill.price * fill.quantity
            self.quantity += fill.quantity
            self.avg_price = total_cost / self.quantity
            return
        if fill.quantity > self.quantity:
            raise ValueError(
                f"Venta de {fill.quantity} {self.symbol} supera la posición ({self.quantity}); "
                "no se permiten cortos"
            )
        self.realized_pnl += (fill.price - self.avg_price) * fill.quantity
        self.quantity -= fill.quantity
        if self.quantity == 0:
            self.avg_price = ZERO
            self.stop_loss = None
            self.take_profit = None
