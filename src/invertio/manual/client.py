"""Signed Revolut X requests, with no automatic retry of a trading operation.

Wire contract: https://developer.revolut.com/docs/api/revolut-x-crypto-exchange.yml
An acknowledgement is not a fill. Callers must reconcile uncertain submissions by
client_order_id and retrieve order details before recording executed quantities.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import httpx
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from invertio.data.throttle import RequestGate

BASE_URL = "https://revx.revolut.com"
_STATES = {"pending_new", "new", "partially_filled", "filled", "cancelled", "rejected", "replaced"}
_ORDER_TYPES = {"market", "limit", "conditional", "tpsl", "twap"}
_CURRENCY = re.compile(r"[A-Z0-9]{1,24}\Z")
_MAX_PAGES = 100


class RevolutXClientError(Exception):
    """Safe to display: no response body, request, API key or signing material."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int | None = None,
        ambiguous: bool = False,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.ambiguous = ambiguous
        self.retry_after_seconds = retry_after_seconds
        messages = {
            "invalid_credentials": "La configuración de autenticación de Revolut X no es válida",
            "invalid_request": "Los parámetros de la petición a Revolut X no son válidos",
            "invalid_response": "Revolut X devolvió una respuesta que no se puede verificar",
            "network_error": "No se pudo confirmar la respuesta de Revolut X",
            "http_error": "Revolut X rechazó la petición",
            "pagination_incomplete": "No se pudo recuperar el listado completo de órdenes",
            "historical_range_too_long": "El historial solicitado supera el límite de 30 días",
        }
        message = messages.get(code, "Error del cliente de Revolut X")
        if status_code is not None:
            message += f" (HTTP {status_code})"
        if ambiguous:
            message += "; el resultado requiere reconciliación antes de volver a operar"
        super().__init__(message)


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("object required")
    return value


def _list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("array required")
    return value


def _currency(value: Any) -> str:
    if not isinstance(value, str) or not _CURRENCY.fullmatch(value):
        raise ValueError("invalid currency")
    return value


def _symbol(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid symbol")
    parts = value.replace("/", "-").split("-")
    if len(parts) != 2:
        raise ValueError("invalid symbol")
    return f"{_currency(parts[0])}/{_currency(parts[1])}"


def _uuid(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 36:
        raise ValueError("invalid UUID")
    return str(UUID(value))


def _amount(value: Any, *, positive: bool = False) -> str:
    # Never accept a JSON float as a monetary amount; decimal strings are the API contract.
    if not isinstance(value, (str, Decimal)) or len(str(value)) > 128:
        raise ValueError("decimal string required")
    amount = Decimal(value)
    if (
        not amount.is_finite()
        or amount < 0
        or (positive and amount <= 0)
        or (amount and abs(amount.adjusted()) > 100)
    ):
        raise ValueError("invalid decimal")
    return format(amount, "f")


def _milliseconds(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("invalid timestamp")
    datetime.fromtimestamp(value / 1000, UTC)
    return value


def _validate[T](parser: Callable[[Any], T], value: Any, *, ambiguous: bool = False) -> T:
    try:
        return parser(value)
    except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError, OSError):
        raise RevolutXClientError("invalid_response", ambiguous=ambiguous) from None


def _order(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    result: dict[str, Any] = {
        "id": _uuid(source["id"]),
        "client_order_id": _uuid(source["client_order_id"]),
        "symbol": _symbol(source["symbol"]),
        "side": source["side"],
        "type": source["type"],
        "status": source["status"],
        "filled_quantity": _amount(source["filled_quantity"]),
        "time_in_force": source["time_in_force"],
        "execution_instructions": _list(source["execution_instructions"]),
        "created_date": _milliseconds(source["created_date"]),
        "updated_date": _milliseconds(source["updated_date"]),
    }
    if (
        result["side"] not in {"buy", "sell"}
        or result["type"] not in _ORDER_TYPES
        or result["status"] not in _STATES
        or result["time_in_force"] not in {"gtc", "ioc", "fok"}
        or any(v not in {"allow_taker", "post_only"} for v in result["execution_instructions"])
    ):
        raise ValueError("unknown order enum")
    for name in (
        "quantity",
        "leaves_quantity",
        "amount",
        "filled_amount",
        "price",
        "average_fill_price",
        "total_fee",
    ):
        if name in source:
            result[name] = _amount(source[name])
    if "fee_currency" in source:
        result["fee_currency"] = _currency(source["fee_currency"])
    if "previous_order_id" in source:
        result["previous_order_id"] = _uuid(source["previous_order_id"])
    # Do not copy arbitrary response strings (e.g. reject_reason) into logs or UI.
    return result


class RevolutXClient:
    def __init__(
        self,
        api_key: str,
        private_key_pem: bytes,
        *,
        client: httpx.AsyncClient | None = None,
        timestamp_ms: Callable[[], int] | None = None,
        request_interval: float = 1.0,
        request_gate: RequestGate | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not re.fullmatch(r"[A-Za-z0-9]{64}", api_key):
            raise RevolutXClientError("invalid_credentials")
        try:
            key = load_pem_private_key(private_key_pem, password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm):
            raise RevolutXClientError("invalid_credentials") from None
        if not isinstance(key, Ed25519PrivateKey):
            raise RevolutXClientError("invalid_credentials")
        if not math.isfinite(request_interval) or request_interval < 0:
            raise RevolutXClientError("invalid_request")
        self._key = key
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(
            timeout=15, follow_redirects=False, trust_env=False
        )
        self._owns_client = client is None
        self._timestamp_ms = timestamp_ms or (lambda: time.time_ns() // 1_000_000)
        self._request_interval = request_interval
        self._request_gate = request_gate
        self._next_request_at = 0.0
        self._lock = asyncio.Lock()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        body: dict[str, Any] | None = None,
        empty: bool = False,
    ) -> Any:
        mutating = method in {"POST", "DELETE"}
        async with self._lock:
            if self._request_gate is not None:
                await self._request_gate.wait()
            else:
                wait = self._next_request_at - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._next_request_at = time.monotonic() + self._request_interval
            payload = json.dumps(body, separators=(",", ":"), ensure_ascii=True) if body else ""
            request = self._client.build_request(
                method,
                f"{BASE_URL}{path}",
                params=params,
                content=payload.encode("utf-8"),
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            stamp = str(self._timestamp_ms())
            raw_path, _, query = request.url.raw_path.partition(b"?")
            message = stamp.encode() + method.encode() + raw_path + query + request.content
            request.headers.update(
                {
                    "X-Revx-API-Key": self._api_key,
                    "X-Revx-Timestamp": stamp,
                    "X-Revx-Signature": base64.b64encode(self._key.sign(message)).decode("ascii"),
                }
            )
            try:
                response = await self._client.send(request, follow_redirects=False)
            except httpx.RequestError:
                raise RevolutXClientError("network_error", ambiguous=mutating) from None
            if response.status_code != (204 if empty else 200):
                retry_after = None
                if response.status_code == 429:
                    try:
                        # Revolut documents an unsigned delay in milliseconds, not seconds.
                        delay = float(response.headers.get("Retry-After", "0")) / 1000
                        if math.isfinite(delay) and delay >= 0:
                            retry_after = delay
                            if self._request_gate is not None:
                                self._request_gate.defer(delay)
                            else:
                                self._next_request_at = max(
                                    self._next_request_at, time.monotonic() + delay
                                )
                    except ValueError:
                        pass
                raise RevolutXClientError(
                    "http_error",
                    status_code=response.status_code,
                    ambiguous=mutating
                    and (
                        response.status_code >= 500
                        or response.status_code in {408, 409}
                        or 200 <= response.status_code < 400
                    ),
                    retry_after_seconds=retry_after,
                )
            if empty:
                return None
            try:
                return response.json()
            except ValueError:
                raise RevolutXClientError("invalid_response", ambiguous=mutating) from None

    async def balances(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "/api/1.0/balances")

        def parse(value: Any) -> list[dict[str, Any]]:
            result = []
            currencies = set()
            for raw in _list(value):
                row = _mapping(raw)
                currency = _currency(row["currency"])
                if currency in currencies:
                    raise ValueError("duplicate balance")
                currencies.add(currency)
                item = {"currency": currency}
                for name in ("available", "reserved", "total"):
                    item[name] = _amount(row[name])
                if "staked" in row:
                    item["staked"] = _amount(row["staked"])
                result.append(item)
            return result

        return _validate(parse, response)

    async def configuration(self) -> dict[str, dict[str, Any]]:
        response = await self._request("GET", "/api/1.0/configuration/pairs")

        def parse(value: Any) -> dict[str, dict[str, Any]]:
            result = {}
            for name, raw in _mapping(value).items():
                row = _mapping(raw)
                symbol = _symbol(name)
                base, quote = _currency(row["base"]), _currency(row["quote"])
                if symbol != f"{base}/{quote}" or symbol in result:
                    raise ValueError("inconsistent pair")
                item = {"base": base, "quote": quote, "status": row["status"]}
                if item["status"] not in {"active", "inactive"}:
                    raise ValueError("unknown market status")
                for field in ("base_step", "quote_step", "max_order_size"):
                    item[field] = _amount(row[field], positive=True)
                for field in ("min_order_size", "min_order_size_quote"):
                    item[field] = _amount(row[field])
                if Decimal(item["min_order_size"]) > Decimal(item["max_order_size"]):
                    raise ValueError("invalid size bounds")
                result[symbol] = item
            return result

        return _validate(parse, response)

    async def order_book(self, symbol: str) -> dict[str, Any]:
        try:
            pair = _symbol(symbol).replace("/", "-")
        except ValueError:
            raise RevolutXClientError("invalid_request") from None
        response = await self._request("GET", f"/api/1.0/order-book/{pair}", params={"limit": 50})

        def parse(value: Any) -> dict[str, Any]:
            wrapper = _mapping(value)
            data, metadata = _mapping(wrapper["data"]), _mapping(wrapper["metadata"])
            stamp = _milliseconds(metadata["timestamp"])
            result: dict[str, Any] = {
                "timestamp": stamp,
                "ts": datetime.fromtimestamp(stamp / 1000, UTC).isoformat(),
                "observed_at": datetime.fromtimestamp(self._timestamp_ms() / 1000, UTC).isoformat(),
                "timestamp_source": "exchange",
            }
            for side in ("bids", "asks"):
                levels = []
                for raw in _list(data[side]):
                    row = _mapping(raw)
                    levels.append(
                        [_amount(row["p"], positive=True), _amount(row["q"], positive=True)]
                    )
                result[side] = sorted(
                    levels, key=lambda row: Decimal(row[0]), reverse=side == "bids"
                )
            if (
                result["bids"]
                and result["asks"]
                and Decimal(result["bids"][0][0]) > Decimal(result["asks"][0][0])
            ):
                raise ValueError("crossed book")
            return result

        return _validate(parse, response)

    async def submit_limit(
        self,
        client_order_id: str,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
    ) -> dict[str, Any]:
        try:
            ident = _uuid(client_order_id)
            pair = _symbol(symbol).replace("/", "-")
            if side not in {"buy", "sell"}:
                raise ValueError("invalid side")
            base_size, limit_price = _amount(quantity, positive=True), _amount(price, positive=True)
        except (ValueError, TypeError, InvalidOperation):
            raise RevolutXClientError("invalid_request") from None
        response = await self._request(
            "POST",
            "/api/1.0/orders",
            body={
                "client_order_id": ident,
                "symbol": pair,
                "side": side,
                "order_configuration": {
                    "limit": {
                        "base_size": base_size,
                        "price": limit_price,
                        "time_in_force": "ioc",
                        "execution_instructions": ["allow_taker"],
                    }
                },
            },
        )

        def parse(value: Any) -> dict[str, Any]:
            data = _mapping(value)["data"]
            if isinstance(data, list):  # Compatibility with the single-item CCXT example.
                if len(data) != 1:
                    raise ValueError("unexpected acknowledgements")
                data = data[0]
            row = _mapping(data)
            client_id = _uuid(row["client_order_id"])
            if client_id != ident or row["state"] not in _STATES:
                raise ValueError("unmatched acknowledgement")
            return {
                "venue_order_id": _uuid(row["venue_order_id"]),
                "client_order_id": client_id,
                "state": row["state"],
            }

        return _validate(parse, response, ambiguous=True)

    async def order(self, venue_order_id: str) -> dict[str, Any]:
        ident = self._order_id(venue_order_id)
        response = await self._request("GET", f"/api/1.0/orders/{ident}")

        def parse(value: Any) -> dict[str, Any]:
            result = _order(_mapping(value)["data"])
            if result["id"] != ident:
                raise ValueError("unmatched order")
            return result

        return _validate(parse, response)

    async def active_orders(self) -> list[dict[str, Any]]:
        return await self._orders("active", {"limit": 300})

    async def historical_orders(self, start_date_ms: int) -> list[dict[str, Any]]:
        try:
            start = _milliseconds(start_date_ms)
        except (ValueError, OverflowError, OSError):
            raise RevolutXClientError("invalid_request") from None
        end = self._timestamp_ms()
        if start > end:
            raise RevolutXClientError("invalid_request")
        if end - start > 30 * 24 * 60 * 60 * 1000:
            raise RevolutXClientError("historical_range_too_long")
        return await self._orders(
            "historical", {"limit": 1900, "start_date": start, "end_date": end}
        )

    async def _orders(self, endpoint: str, params: dict[str, str | int]) -> list[dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        seen_cursors: set[str] = set()
        for _ in range(_MAX_PAGES):
            response = await self._request("GET", f"/api/1.0/orders/{endpoint}", params=params)

            def parse(value: Any) -> tuple[list[dict[str, Any]], str | None]:
                wrapper = _mapping(value)
                rows = [_order(row) for row in _list(wrapper["data"])]
                metadata = _mapping(wrapper["metadata"])
                _milliseconds(metadata["timestamp"])
                cursor = metadata.get("next_cursor")
                if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4096):
                    raise ValueError("invalid cursor")
                return rows, cursor or None

            rows, cursor = _validate(parse, response)
            result.update({row["id"]: row for row in rows})
            if cursor is None:
                return list(result.values())
            if cursor in seen_cursors:
                raise RevolutXClientError("pagination_incomplete")
            seen_cursors.add(cursor)
            params = {**params, "cursor": cursor}
        raise RevolutXClientError("pagination_incomplete")

    @staticmethod
    def _order_id(value: str) -> str:
        try:
            return _uuid(value)
        except ValueError:
            raise RevolutXClientError("invalid_request") from None

    async def cancel(self, venue_order_id: str) -> None:
        ident = self._order_id(venue_order_id)
        await self._request("DELETE", f"/api/1.0/orders/{ident}", empty=True)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
