"""Read-only market data through authenticated Revolut X endpoints.

Public EEA market configuration and tickers remain on the public one-request-per-
second gate. Candle/book reads use a separate conservative five-request-per-second
ceiling, below the documented authenticated market-data limits. This feed never
submits, cancels or inspects account orders.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import partial
from typing import Any, cast

from invertio.analysis.config import AnalysisConfig
from invertio.analysis.market import PublicAnalysisFeed, _number
from invertio.core.models import Bar
from invertio.core.timeframes import timeframe_seconds
from invertio.data.throttle import RequestGate
from invertio.manual.client import RevolutXClient, RevolutXClientError

_PAIR = re.compile(r"[A-Z0-9]{1,24}/EUR\Z")
_MINUTE_MS = 60_000
_PAGE_CANDLES = 1000
# Periodos de vela que documenta Revolut X, en minutos.
_INTERVALS = frozenset({1, 5, 15, 30, 60, 240, 1440})
_AUTH_INTERVAL = 0.2
_MAX_RETRY_DELAY = 5.0


class AuthenticatedAnalysisFeed(PublicAnalysisFeed):
    data_access = "authenticated"
    request_interval_seconds = _AUTH_INTERVAL

    def __init__(
        self,
        config: AnalysisConfig | None,
        client: RevolutXClient,
        *,
        request_gate: RequestGate | None = None,
        exchange: Any | None = None,
        page_cap: int = 2,
    ) -> None:
        super().__init__(config, exchange=exchange, page_cap=page_cap, request_gate=request_gate)
        self._authenticated_client = client
        self._authenticated_lock = asyncio.Lock()
        self._next_authenticated_at = 0.0
        self._authenticated_cooldown_until = 0.0
        self._authenticated_symbols: set[str] = set()
        self.market_verification: dict[str, Any] = {}

    async def _read[T](self, call: Callable[[], Awaitable[T]]) -> T:
        async with self._authenticated_lock:
            for attempt in range(3):
                cooldown = self._authenticated_cooldown_until - time.monotonic()
                if cooldown > _MAX_RETRY_DELAY:
                    raise RevolutXClientError(
                        "http_error", status_code=429, retry_after_seconds=cooldown
                    )
                delay = self._next_authenticated_at - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                self._next_authenticated_at = time.monotonic() + _AUTH_INTERVAL
                try:
                    return await call()
                except RevolutXClientError as exc:
                    if exc.status_code != 429:
                        raise
                    retry_delay = exc.retry_after_seconds
                    if retry_delay is None:
                        retry_delay = 2.0
                    self._next_authenticated_at = max(
                        self._next_authenticated_at, time.monotonic() + retry_delay
                    )
                    self._authenticated_cooldown_until = time.monotonic() + retry_delay
                    # The client's backoff remains in force on subsequent cycles too.
                    if attempt == 2 or retry_delay > _MAX_RETRY_DELAY:
                        raise
        raise AssertionError("unreachable")

    async def markets(self) -> dict[str, dict[str, Any]]:
        public = await super().markets()
        account = await self._read(self._authenticated_client.configuration)
        verified: set[str] = set()
        result: dict[str, dict[str, Any]] = {}
        unavailable: dict[str, str] = {}
        public_eur = 0
        for symbol, market in public.items():
            result[symbol] = dict(market)
            if market.get("quote") != "EUR" or not market.get("spot"):
                continue
            public_eur += 1
            authenticated = account.get(symbol)
            compatible = authenticated is not None and self._compatible(market, authenticated)
            if compatible:
                verified.add(symbol)
            if not compatible or authenticated is None or authenticated["status"] != "active":
                result[symbol]["active"] = False
                reason = (
                    "Par ausente de la configuración autenticada"
                    if authenticated is None
                    else "Configuración pública y autenticada incompatible"
                    if not compatible
                    else "Mercado inactivo en la configuración autenticada"
                )
                result[symbol]["analysis_data_reason"] = reason
                unavailable[symbol] = reason
        self._authenticated_symbols = verified
        self.market_verification = {
            "public_eur": public_eur,
            "verified_eur": len(verified),
            "unavailable": unavailable,
        }
        return result

    @staticmethod
    def _compatible(public: dict[str, Any], authenticated: dict[str, Any]) -> bool:
        if any(public.get(field) != authenticated.get(field) for field in ("base", "quote")):
            return False
        # CCXT preserves the original decimal strings in info. Compare those rather
        # than rounded binary precision values, and never adopt account-only pairs.
        original = public.get("info")
        if not isinstance(original, dict):
            return False
        for field in (
            "base_step",
            "quote_step",
            "min_order_size",
            "max_order_size",
            "min_order_size_quote",
        ):
            try:
                left = Decimal(str(original[field]))
                right = Decimal(str(authenticated[field]))
                if not left.is_finite() or not right.is_finite() or left != right:
                    return False
            except (KeyError, InvalidOperation, ValueError):
                return False
        return True

    def _pair(self, symbol: str) -> str:
        if not isinstance(symbol, str) or not _PAIR.fullmatch(symbol):
            raise RevolutXClientError("invalid_request")
        if symbol not in self._authenticated_symbols:
            raise RevolutXClientError("invalid_request")
        return symbol.replace("/", "-")

    @staticmethod
    def _response(response: Any) -> tuple[list[Any], int]:
        if not isinstance(response, dict):
            raise RevolutXClientError("invalid_response")
        data, metadata = response.get("data"), response.get("metadata")
        if not isinstance(data, list) or not isinstance(metadata, dict):
            raise RevolutXClientError("invalid_response")
        stamp = metadata.get("timestamp")
        if (
            not isinstance(stamp, int)
            or isinstance(stamp, bool)
            or stamp < 0
            or metadata.get("region", "EEA") != "EEA"
        ):
            raise RevolutXClientError("invalid_response")
        try:
            datetime.fromtimestamp(stamp / 1000, UTC)
        except (ValueError, OverflowError, OSError):
            raise RevolutXClientError("invalid_response") from None
        return data, stamp

    async def bars(
        self, symbol: str, since: datetime, now: datetime, *, timeframe: str = "1m"
    ) -> list[Bar]:
        if since.tzinfo is None or now.tzinfo is None:
            raise ValueError("since y now necesitan zona horaria")
        step = timeframe_seconds(timeframe) * 1000
        if step % _MINUTE_MS or step // _MINUTE_MS not in _INTERVALS:
            raise ValueError(f"Revolut X no ofrece velas de {timeframe}")
        pair = self._pair(symbol)
        cursor, end = int(since.timestamp() * 1000), int(now.timestamp() * 1000)
        if cursor < 0:
            raise RevolutXClientError("invalid_request")
        bars: dict[int, Bar] = {}
        for _ in range(self._page_cap):
            if cursor >= end:
                break
            until = min(cursor + _PAGE_CANDLES * step, end)
            response = await self._read(
                partial(
                    self._authenticated_client._request,
                    "GET",
                    f"/api/1.0/candles/{pair}",
                    params={
                        "interval": step // _MINUTE_MS,
                        "since": cursor,
                        "until": until - 1,
                    },
                )
            )
            rows, source_at = self._response(response)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                start = row.get("start")
                if (
                    not isinstance(start, int)
                    or isinstance(start, bool)
                    or start % step != 0
                    or not cursor <= start < until
                    or start + step > min(end, source_at)
                ):
                    continue
                values = [
                    _number(row.get(field)) for field in ("open", "high", "low", "close", "volume")
                ]
                if any(value is None for value in values):
                    continue
                opening, high, low, close, volume = cast(list[float], values)
                if (
                    min(opening, high, low, close) <= 0
                    or low > min(opening, close)
                    or high < max(opening, close)
                ):
                    continue
                bars[start] = Bar(
                    venue="revolutx",
                    symbol=symbol,
                    timeframe=timeframe,
                    open_time=datetime.fromtimestamp(start / 1000, UTC),
                    open=opening,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                )
            cursor = until
        return [bars[start] for start in sorted(bars)]

    async def book(self, symbol: str) -> dict[str, Any]:
        self._pair(symbol)
        # Preserve exchange time and exact decimal strings. Both analysis cost
        # estimation and fictitious portfolios already accept these numeric strings.
        return await self._read(lambda: self._authenticated_client.order_book(symbol))

    async def close(self) -> None:
        try:
            await self._authenticated_client.close()
        finally:
            await super().close()
