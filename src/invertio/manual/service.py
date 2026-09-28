"""Previsualización inmutable, confirmación e identificación duradera de órdenes.

No recibe señales ni se suscribe al motor. Únicamente confirm() y cancel(),
invocados por botones protegidos, pueden modificar la cuenta del exchange.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Protocol
from uuid import uuid4

from invertio.manual.repository import ManualRepository

ZERO = Decimal("0")
FEE = Decimal("0.0009")
BUDGET = Decimal("50")
UNRESOLVED = {"submitting", "unknown", "pending"}


class ManualError(ValueError):
    """Mensaje público y sin credenciales."""


class ManualClient(Protocol):
    async def balances(self) -> list[dict[str, Any]]: ...
    async def configuration(self) -> dict[str, dict[str, Any]]: ...
    async def order_book(self, symbol: str) -> dict[str, Any]: ...
    async def submit_limit(
        self, client_order_id: str, symbol: str, side: str, quantity: Decimal, price: Decimal
    ) -> dict[str, Any]: ...
    async def order(self, venue_order_id: str) -> dict[str, Any]: ...
    async def active_orders(self) -> list[dict[str, Any]]: ...
    async def historical_orders(self, start_date_ms: int) -> list[dict[str, Any]]: ...
    async def cancel(self, venue_order_id: str) -> None: ...
    async def close(self) -> None: ...


def number(value: Any, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ManualError("Importe no válido")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ManualError("Importe no válido") from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ManualError("El importe debe ser finito y positivo")
    return result


def stamp(value: str) -> datetime:
    at = datetime.fromisoformat(value)
    if at.tzinfo is None:
        raise ManualError("Fecha sin zona horaria")
    return at


def rounded(value: Decimal, step: Decimal, *, up: bool = False) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * step


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


class ManualService:
    def __init__(
        self,
        repository: ManualRepository,
        client: ManualClient | None,
        *,
        enabled: bool = False,
        account_id: str = "unconfigured",
        clock: Callable[[], datetime] | None = None,
        setup_error: str | None = None,
    ) -> None:
        self.repository, self.client = repository, client
        self.enabled, self.account_id = enabled, account_id
        self.clock = clock or (lambda: datetime.now(UTC))
        self.error = setup_error
        self._balances: list[dict[str, Any]] = []
        self._balances_at: str | None = None
        self._rules: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    def _client(self) -> ManualClient:
        if self.client is None:
            raise ManualError(self.error or "Faltan las claves de Revolut X en .env")
        return self.client

    async def _orders(self) -> list[dict[str, Any]]:
        return [r for r in await self.repository.orders() if r.get("account_id") == self.account_id]

    async def _account_guard(self) -> None:
        # La huella identifica la clave, no al titular. Rotarla no debe resetear esta prueba.
        if any(
            r.get("account_id") != self.account_id and r["status"] not in {"preview", "expired"}
            for r in await self.repository.orders()
        ):
            raise ManualError(
                "La clave API ha cambiado y existe historial manual. "
                "Hay que conciliar ese historial antes de operar con otra clave."
            )

    def _ledger(self, rows: list[dict[str, Any]]) -> tuple[Decimal, dict[str, Decimal]]:
        committed = ZERO
        holdings: dict[str, Decimal] = {}
        for row in rows:
            filled = number(row["filled_quantity"])
            symbol = row["symbol"]
            execution = row.get("execution") or {}
            base_fee = (
                number(execution.get("total_fee", "0"))
                if execution.get("fee_currency") == symbol.split("/")[0]
                else ZERO
            )
            holdings[symbol] = holdings.get(symbol, ZERO) + (
                filled - base_fee if row["side"] == "buy" else -filled - base_fee
            )
            if row["side"] == "buy":
                if row["status"] in UNRESOLVED:
                    committed += number(row["max_debit_eur"])
                elif filled:
                    # Cota conservadora del gasto; nunca cuenta beneficios hipotéticos.
                    notional = filled * number(row["limit_price"])
                    estimate = money(notional + money(notional * FEE))
                    actual = number(execution.get("filled_amount", "0"))
                    if execution.get("fee_currency") == "EUR":
                        actual += number(execution.get("total_fee", "0"))
                    committed += max(estimate, money(actual))
        return committed, holdings

    async def status(self) -> dict[str, Any]:
        rows = await self._orders()
        committed, holdings = self._ledger(rows)
        identity_error = None
        try:
            await self._account_guard()
        except ManualError as exc:
            identity_error = str(exc)
        return {
            "available": True,
            "configured": self.client is not None,
            "enabled": self.enabled and identity_error is None,
            "budget_eur": str(BUDGET),
            "committed_eur": str(committed),
            "remaining_eur": str(max(ZERO, BUDGET - committed)),
            "balances": self._balances,
            "balances_updated_at": self._balances_at,
            "positions": [{"symbol": s, "quantity": str(q)} for s, q in holdings.items() if q > 0],
            "orders": rows[:100],
            "error": identity_error or self.error,
        }

    async def markets(self) -> dict[str, Any]:
        async with self._lock:
            self._rules = await self._client().configuration()
            return {
                "symbols": sorted(
                    s
                    for s, r in self._rules.items()
                    if s.endswith("/EUR") and r.get("status") == "active"
                )
            }

    def _available(self, currency: str) -> Decimal:
        return next(
            (number(b["available"]) for b in self._balances if b["currency"] == currency), ZERO
        )

    async def _load_balances(self) -> None:
        try:
            self._balances = await self._client().balances()
            self._balances_at = self.clock().isoformat()
        except Exception:
            self._balances = []
            self._balances_at = None
            raise

    def _apply_exchange(self, row: dict[str, Any], remote: dict[str, Any]) -> None:
        symbol = str(remote.get("symbol", "")).replace("-", "/")
        if (
            remote.get("client_order_id") != row["id"]
            or symbol != row["symbol"]
            or str(remote.get("side", "")).lower() != row["side"]
            or number(remote.get("quantity")) != number(row["quantity"])
            or remote.get("type") != "limit"
            or remote.get("time_in_force") != "ioc"
            or number(remote.get("price")) != number(row["limit_price"])
        ):
            raise ManualError("El exchange devolvió una orden que no coincide con la enviada")
        filled = number(remote.get("filled_quantity"))
        if filled > number(row["quantity"]) or filled < number(row["filled_quantity"]):
            raise ManualError("Cantidad ejecutada incoherente; revisar la orden en Revolut X")
        if remote.get("status") == "filled" and filled != number(row["quantity"]):
            raise ManualError("Estado ejecutado con cantidad incompleta; revisar en Revolut X")
        if filled:
            fee = number(remote.get("total_fee"))
            if fee and remote.get("fee_currency") not in {"EUR", row["symbol"].split("/")[0]}:
                raise ManualError("Comisión desconocida; revisar en Revolut X")
            if remote.get("fee_currency") == row["symbol"].split("/")[0] and fee >= filled:
                raise ManualError("Comisión incoherente; revisar en Revolut X")
            average = number(remote.get("average_fill_price"), positive=True)
            amount = number(remote.get("filled_amount"), positive=True)
            limit = number(row["limit_price"])
            if (
                (row["side"] == "buy" and average > limit)
                or (row["side"] == "sell" and average < limit)
                or abs(amount - average * filled) > Decimal("0.01")
            ):
                raise ManualError(
                    "Ejecución incompatible con el precio límite; revisar en Revolut X"
                )
        elif (
            number(remote.get("filled_amount", "0")) != 0
            or number(remote.get("total_fee", "0")) != 0
        ):
            raise ManualError("Importe o comisión sin cantidad ejecutada; revisar en Revolut X")
        exchange_status = str(remote.get("status", "")).lower()
        row["exchange_id"] = str(remote["id"])
        row["exchange_status"] = exchange_status
        row["filled_quantity"] = str(filled)
        row["status"] = {
            "filled": "filled",
            "cancelled": "cancelled",
            "canceled": "cancelled",
            "rejected": "rejected",
            "expired": "cancelled",
        }.get(exchange_status, "pending")
        row["reason"] = None
        # Solo datos del exchange: no confundir previsiones con ejecución real.
        row["execution"] = {
            key: remote.get(key)
            for key in ("average_fill_price", "filled_amount", "total_fee", "fee_currency")
        }

    async def _reconcile(self) -> None:
        await self._account_guard()
        client = self._client()
        rows = [r for r in await self._orders() if r["status"] in UNRESOLVED]
        for row in rows:
            try:
                remote = None
                if row.get("exchange_id"):
                    remote = await client.order(row["exchange_id"])
                else:
                    start = int(
                        (stamp(row["created_at"]) - timedelta(minutes=1)).timestamp() * 1000
                    )
                    known = await client.active_orders() + await client.historical_orders(start)
                    remote = next((r for r in known if r.get("client_order_id") == row["id"]), None)
                    if remote is not None:
                        remote = await client.order(str(remote["id"]))
                if remote is not None:
                    self._apply_exchange(row, remote)
                else:
                    row.update(
                        status="unknown",
                        reason="Sin confirmación del exchange. "
                        "Se bloquean nuevas órdenes; comprueba Revolut X y actualiza.",
                    )
                await self.repository.save(row)
            except Exception:
                # Un GET fallido no demuestra que el POST no llegase.
                self.error = "No se pudo conciliar una orden; sigue reservada y no se reenviará"

    async def refresh(self) -> dict[str, Any]:
        async with self._lock:
            self.error = None
            try:
                await self._reconcile()
                await self._load_balances()
            except ManualError as exc:
                self._balances = []
                self._balances_at = None
                self.error = str(exc)
            except Exception:
                self._balances = []
                self._balances_at = None
                self.error = (
                    "No se pudo consultar Revolut X. Revisa la conexión y los permisos API."
                )
            return await self.status()

    def _rule(self, symbol: str) -> dict[str, Any]:
        rule = self._rules.get(symbol)
        if not symbol.endswith("/EUR") or rule is None or rule.get("status") != "active":
            raise ManualError("Selecciona un mercado EUR activo de Revolut X")
        return rule

    def _book(
        self, book: dict[str, Any]
    ) -> tuple[list[tuple[Decimal, Decimal]], list[tuple[Decimal, Decimal]]]:
        age = self.clock() - stamp(str(book["ts"]))
        if not timedelta(0) <= age <= timedelta(seconds=10):
            raise ManualError("El libro está desactualizado; vuelve a preparar la orden")
        asks = sorted((number(p, positive=True), number(q, positive=True)) for p, q in book["asks"])
        bids = sorted(
            ((number(p, positive=True), number(q, positive=True)) for p, q in book["bids"]),
            reverse=True,
        )
        if not asks or not bids or bids[0][0] > asks[0][0]:
            raise ManualError("Libro de órdenes no válido")
        spread = (asks[0][0] - bids[0][0]) / ((asks[0][0] + bids[0][0]) / 2)
        if spread > Decimal("0.003"):
            raise ManualError("El spread supera el 0,3 %")
        return asks, bids

    def _depth(self, row: dict[str, Any], book: dict[str, Any]) -> None:
        asks, bids = self._book(book)
        price, quantity = number(row["limit_price"]), number(row["quantity"])
        levels = asks if row["side"] == "buy" else bids
        available = sum(
            (q for p, q in levels if (p <= price if row["side"] == "buy" else p >= price)), ZERO
        )
        if available < quantity:
            raise ManualError("No hay profundidad suficiente dentro del precio límite")

    def _validate_size(self, row: dict[str, Any], rule: dict[str, Any]) -> None:
        quantity, price = (
            number(row["quantity"], positive=True),
            number(row["limit_price"], positive=True),
        )
        if (
            quantity != rounded(quantity, number(rule["base_step"], positive=True))
            or price != rounded(price, number(rule["quote_step"], positive=True))
            or quantity < number(rule["min_order_size"])
            or quantity > number(rule["max_order_size"], positive=True)
            or quantity * price < number(rule["min_order_size_quote"])
        ):
            raise ManualError("El importe no cumple los mínimos o incrementos del mercado")

    async def _check_funds(self, row: dict[str, Any]) -> None:
        rows = await self._orders()
        if any(r["status"] in UNRESOLVED for r in rows):
            raise ManualError(
                "Hay una orden pendiente o incierta. Actualiza su estado antes de continuar"
            )
        committed, holdings = self._ledger(rows)
        if row["side"] == "buy":
            debit = number(row["max_debit_eur"])
            if debit > self._available("EUR") or committed + debit > BUDGET:
                raise ManualError("Saldo EUR insuficiente o límite acumulado de 50 € superado")
        else:
            quantity = number(row["quantity"])
            if quantity > min(
                holdings.get(row["symbol"], ZERO), self._available(row["symbol"].split("/")[0])
            ):
                raise ManualError(
                    "Solo puedes vender la cantidad disponible comprada desde este panel"
                )

    async def preview(
        self, symbol: str, side: str, *, budget_eur: str | None = None, quantity: str | None = None
    ) -> dict[str, Any]:
        async with self._lock:
            client = self._client()
            if side not in {"buy", "sell"}:
                raise ManualError("Lado de la orden no válido")
            await self._reconcile()
            self._rules = await client.configuration()
            rule = self._rule(symbol)
            await self._load_balances()
            book = await client.order_book(symbol)
            asks, bids = self._book(book)
            buy = side == "buy"
            price = rounded(
                (asks[0][0] * Decimal("1.001") if buy else bids[0][0] * Decimal("0.999")),
                number(rule["quote_step"], positive=True),
                up=not buy,
            )
            step = number(rule["base_step"], positive=True)
            if buy:
                budget = number(budget_eur, positive=True)
                if budget > BUDGET or budget != money(budget):
                    raise ManualError("El presupuesto máximo de esta prueba es de 50 €")
                qty = rounded(budget / (price * (1 + FEE)), step)
                # Incluye el redondeo conservador de la comisión en EUR.
                if qty * price + money(qty * price * FEE) > budget:
                    qty = rounded((budget - Decimal("0.01")) / (price * (1 + FEE)), step)
            else:
                qty = rounded(number(quantity, positive=True), step)
            notional = qty * price
            fee = money(notional * FEE)
            now = self.clock()
            row = {
                "id": str(uuid4()),
                "account_id": self.account_id,
                "symbol": symbol,
                "side": side,
                "quantity": str(qty),
                "limit_price": str(price),
                "notional_eur": str(notional),
                "fee_reserve_eur": str(fee),
                "max_debit_eur": str(money(notional + fee) if buy else ZERO),
                "estimated_proceeds_eur": str(max(ZERO, notional - fee) if not buy else ZERO),
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=30)).isoformat(),
                "status": "preview",
                "exchange_id": None,
                "exchange_status": None,
                "filled_quantity": "0",
                "reason": None,
            }
            self._validate_size(row, rule)
            self._depth(row, book)
            await self._check_funds(row)
            await self.repository.save(row)
            return row

    async def confirm(self, preview_id: str) -> dict[str, Any]:
        async with self._lock:
            await self._account_guard()
            if not self.enabled:
                raise ManualError(
                    "Órdenes reales desactivadas; inicia análisis con --manual-orders"
                )
            client = self._client()
            row = next((r for r in await self._orders() if r["id"] == preview_id), None)
            if row is None:
                raise ManualError("Previsualización desconocida")
            # Doble clic, pérdida de respuesta o reinicio: nunca volver a enviar.
            if row["status"] != "preview":
                return row
            if self.clock() >= stamp(row["expires_at"]):
                row.update(status="expired", reason="La previsualización ha caducado")
                await self.repository.save(row)
                return row
            await self._reconcile()
            self._rules = await client.configuration()
            self._validate_size(row, self._rule(row["symbol"]))
            await self._load_balances()
            self._depth(row, await client.order_book(row["symbol"]))
            await self._check_funds(row)
            if self.clock() >= stamp(row["expires_at"]):
                raise ManualError("La previsualización ha caducado; prepara otra")
            row["status"] = "submitting"
            await self.repository.save(row)
            try:
                ack = await client.submit_limit(
                    row["id"],
                    row["symbol"],
                    row["side"],
                    number(row["quantity"]),
                    number(row["limit_price"]),
                )
                row.update(status="pending", exchange_id=ack["venue_order_id"], reason=None)
            except Exception as exc:
                # La ausencia de acuse no permite asumir que no existe una orden.
                ambiguous = getattr(exc, "ambiguous", True)
                row.update(
                    status="unknown" if ambiguous else "rejected",
                    reason="Envío sin confirmación; actualiza para conciliar con Revolut X"
                    if ambiguous
                    else "Revolut X rechazó el envío de la orden",
                )
            await self.repository.save(row)
            await self._reconcile()
            return next(r for r in await self._orders() if r["id"] == preview_id)

    async def cancel(self, order_id: str) -> dict[str, Any]:
        async with self._lock:
            await self._account_guard()
            if not self.enabled:
                raise ManualError("Órdenes reales desactivadas")
            row = next((r for r in await self._orders() if r["id"] == order_id), None)
            if row is None or not row.get("exchange_id"):
                raise ManualError("No hay una orden identificada que se pueda cancelar")
            if row["status"] not in UNRESOLVED:
                return row
            try:
                await self._client().cancel(row["exchange_id"])
            except Exception:
                row["reason"] = "Cancelación sin confirmar; actualiza el estado antes de continuar"
                await self.repository.save(row)
            await self._reconcile()
            return next(r for r in await self._orders() if r["id"] == order_id)

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()
