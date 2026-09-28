"""Datos públicos, universo EUR y costes basados en profundidad observable."""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, cast

from ccxt.base.errors import RateLimitExceeded

from invertio.analysis.config import AnalysisConfig
from invertio.core.models import Bar
from invertio.core.timeframes import timeframe_seconds
from invertio.data.ccxt_history import create_exchange
from invertio.data.throttle import RequestGate
from invertio.live.feeds import STABLECOINS


class AnalysisFeed(Protocol):
    async def markets(self) -> dict[str, dict[str, Any]]: ...

    async def tickers(self) -> dict[str, dict[str, Any]]: ...

    async def bars(
        self, symbol: str, since: datetime, now: datetime, *, timeframe: str = "1m"
    ) -> list[Bar]: ...

    async def book(self, symbol: str) -> dict[str, Any]: ...

    async def close(self) -> None: ...


def _number(value: Any, *, positive: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0 or (positive and number == 0):
        return None
    return number


def _stamp(data: dict[str, Any], observed: datetime) -> dict[str, str]:
    """Preserva timestamps antiguos; la recepción es un dato distinto de la fuente."""
    timestamp = _number(data.get("timestamp"))
    source = "observed"
    value = observed
    if timestamp is not None:
        try:
            value = datetime.fromtimestamp(timestamp / 1000, UTC)
            source = "exchange"
        except (ValueError, OverflowError, OSError):
            # Un timestamp presente pero inválido no debe aparentar datos recientes.
            value = datetime(1970, 1, 1, tzinfo=UTC)
            source = "invalid"
    elif data.get("timestamp") is not None:
        value = datetime(1970, 1, 1, tzinfo=UTC)
        source = "invalid"
    return {
        "ts": value.isoformat(),
        "observed_at": observed.isoformat(),
        "timestamp_source": source,
    }


def normalize_ticker(ticker: dict[str, Any], observed: datetime) -> dict[str, Any]:
    bid = _number(ticker.get("bid"), positive=True)
    ask = _number(ticker.get("ask"), positive=True)
    volume = ticker.get("quoteVolume")
    if volume is None:
        info = ticker.get("info")
        volume = info.get("quote_volume_24h") if isinstance(info, dict) else None
    return {
        "bid": bid,
        "ask": ask,
        "quote_volume": _number(volume),
        "spread_pct": (ask - bid) / ((ask + bid) / 2) * 100
        if bid is not None and ask is not None and ask >= bid
        else None,
        **_stamp(ticker, observed),
    }


def _retry_after(headers: dict[str, Any] | None, now: datetime) -> float:
    lower = {str(k).lower(): str(v).strip() for k, v in (headers or {}).items()}
    milliseconds = lower.get("retry-after-ms") or lower.get("x-retry-after-ms")
    if milliseconds is not None:
        numeric = _number(milliseconds)
        if numeric is not None:
            return numeric / 1000
    value = lower.get("retry-after", "")
    numeric_match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ms|s)?", value, re.IGNORECASE)
    if numeric_match:
        delay = float(numeric_match[1])
        # Revolut X documenta Retry-After en milisegundos, incluso sin sufijo.
        return delay if (numeric_match[2] or "").lower() == "s" else delay / 1000
    try:
        return max(0.0, (parsedate_to_datetime(value).astimezone(UTC) - now).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return 2.0


class PublicAnalysisFeed:
    """Un cliente, un límite de 1 petición/s incluso durante load_markets de CCXT.

    La envoltura está en el transporte: CCXT puede cargar monedas y mercados en la
    misma llamada y cualquier reintento también debe pasar por este límite.
    """

    def __init__(
        self,
        config: AnalysisConfig | None = None,
        *,
        exchange: Any | None = None,
        page_cap: int = 2,
        request_gate: RequestGate | None = None,
    ) -> None:
        if page_cap < 1:
            raise ValueError("page_cap debe ser positivo")
        self.config = config or AnalysisConfig()
        self._exchange = exchange if exchange is not None else create_exchange("revolutx")
        self._exchange.options = {**getattr(self._exchange, "options", {}), "region": "EEA"}
        self._exchange.enableLastResponseHeaders = True
        self._lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._page_cap = page_cap
        self._request_gate = request_gate
        transport = getattr(self._exchange, "fetch", None)
        self._wrapped_transport = callable(transport)
        if self._wrapped_transport:
            transport_call = cast(Callable[..., Awaitable[Any]], transport)

            async def limited_fetch(*args: Any, **kwargs: Any) -> Any:
                return await self._limited_call(transport_call, *args, **kwargs)

            self._exchange.fetch = limited_fetch

    async def _limited_call(
        self, call: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any
    ) -> Any:
        async with self._lock:
            for attempt in range(3):
                if self._request_gate is not None:
                    await self._request_gate.wait()
                else:
                    wait = self._next_request_at - time.monotonic()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    self._next_request_at = time.monotonic() + 1.0
                try:
                    return await call(*args, **kwargs)
                except RateLimitExceeded:
                    delay = _retry_after(
                        getattr(self._exchange, "last_response_headers", None), datetime.now(UTC)
                    )
                    if not math.isfinite(delay):
                        delay = 2.0
                    if self._request_gate is not None:
                        self._request_gate.defer(delay)
                    else:
                        self._next_request_at = max(self._next_request_at, time.monotonic() + delay)
                    if attempt == 2 or delay > 300:
                        raise
        raise AssertionError("unreachable")

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        call = getattr(self._exchange, method)
        if self._wrapped_transport:
            return await call(*args, **kwargs)
        return await self._limited_call(call, *args, **kwargs)

    async def markets(self) -> dict[str, dict[str, Any]]:
        # La selección se revisa cada cinco minutos: invalidar la caché de CCXT
        # permite detectar mercados desactivados o incorporados desde el arranque.
        return cast(dict[str, dict[str, Any]], await self._call("load_markets", reload=True))

    async def tickers(self) -> dict[str, dict[str, Any]]:
        raw = await self._call("fetch_tickers")
        observed = datetime.now(UTC)
        return {symbol: normalize_ticker(ticker, observed) for symbol, ticker in raw.items()}

    async def bars(
        self, symbol: str, since: datetime, now: datetime, *, timeframe: str = "1m"
    ) -> list[Bar]:
        if since.tzinfo is None or now.tzinfo is None:
            raise ValueError("since y now necesitan zona horaria")
        step = timeframe_seconds(timeframe) * 1000
        cursor = int(since.timestamp() * 1000)
        end = int(now.timestamp() * 1000)
        bars: dict[int, Bar] = {}
        for _ in range(self._page_cap):
            if cursor >= end:
                break
            until = min(cursor + 1000 * step, end)
            rows = await self._call(
                "fetch_ohlcv",
                symbol,
                timeframe,
                since=cursor,
                limit=1000,
                params={"until": until - 1},
            )
            for row in rows:
                if len(row) < 6:
                    continue
                values = [_number(value) for value in row[:6]]
                if any(value is None for value in values):
                    continue
                ts, o, high, low, close, volume = cast(list[float], values)
                if (
                    not cursor <= ts < until
                    or ts + step > end
                    or ts % step != 0
                    or min(o, high, low, close) <= 0
                    or low > min(o, close)
                    or high < max(o, close)
                ):
                    continue
                bars[int(ts)] = Bar(
                    venue="revolutx",
                    symbol=symbol,
                    timeframe=timeframe,
                    open_time=datetime.fromtimestamp(ts / 1000, UTC),
                    open=o,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                )
            cursor = until
        return [bars[ts] for ts in sorted(bars)]

    async def book(self, symbol: str) -> dict[str, Any]:
        raw = await self._call("fetch_order_book", symbol)
        return {
            "bids": raw.get("bids", []),
            "asks": raw.get("asks", []),
            **_stamp(raw, datetime.now(UTC)),
        }

    async def close(self) -> None:
        await self._exchange.close()


def select_markets(
    markets: dict[str, dict[str, Any]],
    tickers: dict[str, dict[str, Any]],
    config: AnalysisConfig,
    held: list[str],
) -> tuple[list[str], list[dict[str, Any]]]:
    reserved = list(dict.fromkeys(held))
    if config.market_scope == "top":
        reserved = reserved[: config.max_markets]
    eligible: list[tuple[str, float]] = []
    observed: list[tuple[str, float]] = []
    excluded: list[dict[str, Any]] = []
    for symbol, market in markets.items():
        ticker = tickers.get(symbol, {})
        reasons = []
        volume = _number(ticker.get("quote_volume"), positive=True)
        bid = _number(ticker.get("bid"), positive=True)
        ask = _number(ticker.get("ask"), positive=True)
        spread = (ask - bid) / ((ask + bid) / 2) * 100 if bid and ask and ask >= bid else None
        if (
            market.get("quote") == config.quote
            and market.get("spot")
            and market.get("active") is True
        ):
            observed.append((symbol, volume or 0.0))
        if market.get("quote") != config.quote:
            reasons.append("Cotización distinta de EUR")
        if not market.get("spot"):
            reasons.append("Mercado no al contado")
        if market.get("active") is not True:
            reasons.append("Mercado inactivo o actividad desconocida")
        if market.get("analysis_data_reason"):
            reasons.append(market["analysis_data_reason"])
        if market.get("base") in STABLECOINS:
            reasons.append("Stablecoin excluida")
        if volume is None:
            reasons.append("Volumen EUR desconocido o nulo")
        if spread is None:
            reasons.append("Cotización bid/ask inválida")
        elif spread > config.max_spread_pct:
            reasons.append("Spread superior al límite")
        if reasons:
            excluded.append(
                {
                    "symbol": symbol,
                    "market": f"revolutx:{symbol}",
                    "reasons": reasons,
                    "quote_volume": volume,
                    "spread_pct": spread,
                    "held": symbol in reserved,
                }
            )
        else:
            assert volume is not None
            eligible.append((symbol, volume))
    if config.market_scope == "all_eur":
        observed.sort(key=lambda item: (-item[1], item[0]))
        return list(dict.fromkeys(reserved + [symbol for symbol, _ in observed])), excluded
    eligible.sort(key=lambda item: (-item[1], item[0]))
    selected = list(reserved)
    for symbol, volume in eligible:
        if symbol in selected:
            continue
        if len(selected) < config.max_markets:
            selected.append(symbol)
        else:
            excluded.append(
                {
                    "symbol": symbol,
                    "market": f"revolutx:{symbol}",
                    "reasons": ["Fuera de las plazas por volumen"],
                    "quote_volume": volume,
                    "held": False,
                }
            )
    return selected, excluded


def _levels(raw: Any, *, reverse: bool) -> list[tuple[float, float]] | None:
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    levels = []
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            return None
        price, quantity = _number(row[0], positive=True), _number(row[1], positive=True)
        if price is None or quantity is None:
            return None
        levels.append((price, quantity))
    return sorted(levels, reverse=reverse)


def estimate_costs(book: dict[str, Any], config: AnalysisConfig, atr: float) -> dict[str, Any]:
    result: dict[str, Any] = {
        "eligible": False,
        "reasons": [],
        "entry": None,
        "stop": None,
        "target": None,
        "quantity": None,
        "round_trip_cost_pct": None,
        "entry_fee_eur": None,
        "round_trip_cost_eur": None,
        "spread_pct": None,
        "buy_slippage_pct": None,
        "sell_slippage_pct": None,
        "target_net_pct": None,
        "exit_estimate": None,
        "exit_fee_eur": None,
        "reference_eur": config.reference_eur,
        "exit_price_factor": None,
        "fee_pct": config.taker_fee_pct,
        "cost_model": "book_vwap_mid_exit_projection_v1",
        **{key: book.get(key) for key in ("ts", "observed_at", "timestamp_source")},
    }
    reasons: list[str] = result["reasons"]
    bids, asks = _levels(book.get("bids"), reverse=True), _levels(book.get("asks"), reverse=False)
    if bids is None or asks is None or asks[0][0] < bids[0][0]:
        reasons.append("Libro vacío, cruzado o con niveles inválidos")
        return result
    if _number(atr, positive=True) is None:
        reasons.append("ATR inválido o nulo")
        return result
    best_bid, best_ask = bids[0][0], asks[0][0]
    spread = (best_ask - best_bid) / ((best_ask + best_bid) / 2) * 100
    result["spread_pct"] = spread
    if spread > config.max_spread_pct:
        reasons.append("Spread superior al límite")
    remaining_eur = config.reference_eur
    quantity = 0.0
    for price, available in asks:
        spent = min(remaining_eur, price * available)
        quantity += spent / price
        remaining_eur -= spent
        if remaining_eur <= config.reference_eur * 1e-12:
            break
    if remaining_eur > config.reference_eur * 1e-12 or quantity <= 0:
        reasons.append("Profundidad insuficiente para comprar")
        return result
    remaining_quantity = quantity
    proceeds = 0.0
    for price, available in bids:
        sold = min(remaining_quantity, available)
        proceeds += sold * price
        remaining_quantity -= sold
        if remaining_quantity <= quantity * 1e-12:
            break
    if remaining_quantity > quantity * 1e-12:
        reasons.append("Profundidad insuficiente para vender la cantidad comprada")
        return result
    entry = config.reference_eur / quantity
    exit_estimate = proceeds / quantity
    buy_slippage = max(0.0, (entry / best_ask - 1) * 100)
    sell_slippage = max(0.0, (1 - exit_estimate / best_bid) * 100)
    fee_rate = config.taker_fee_pct / 100
    entry_fee = config.reference_eur * fee_rate
    exit_fee = proceeds * fee_rate
    # Spread y ambos impactos YA están en la diferencia entre VWAPs.
    round_trip = config.reference_eur - proceeds + entry_fee + exit_fee
    stop, target = entry - config.stop_atr * atr, entry + config.target_atr * atr
    # Objetivo: precio indicativo del mercado. La entrada ya incluye el impacto
    # comprador; solo se proyecta el coste vendedor desde el mid hacia su VWAP.
    exit_price_factor = exit_estimate / ((best_ask + best_bid) / 2)
    projected_exit = target * exit_price_factor
    target_net = (
        (quantity * projected_exit * (1 - fee_rate) - config.reference_eur - entry_fee)
        / config.reference_eur
        * 100
    )
    result.update(
        entry=entry,
        stop=stop,
        target=target,
        quantity=quantity,
        exit_estimate=exit_estimate,
        buy_slippage_pct=buy_slippage,
        sell_slippage_pct=sell_slippage,
        entry_fee_eur=entry_fee,
        exit_fee_eur=exit_fee,
        round_trip_cost_eur=round_trip,
        round_trip_cost_pct=round_trip / config.reference_eur * 100,
        target_net_pct=target_net,
        exit_price_factor=exit_price_factor,
    )
    if buy_slippage > config.max_slippage_pct + 1e-10:
        reasons.append("Deslizamiento de compra superior al límite")
    if sell_slippage > config.max_slippage_pct + 1e-10:
        reasons.append("Deslizamiento de venta superior al límite")
    if stop <= 0:
        reasons.append("Stop no positivo")
    if target_net <= 0:
        reasons.append("Objetivo neto no positivo después de costes")
    if not all(math.isfinite(value) for value in result.values() if isinstance(value, float)):
        reasons.append("Cálculo de costes no finito")
        for key, value in list(result.items()):
            if isinstance(value, float) and not math.isfinite(value):
                result[key] = None
    result["eligible"] = not reasons
    return result


def estimate_tracking_costs(book: dict[str, Any], opportunity: dict[str, Any]) -> dict[str, Any]:
    """Proyecta la salida del objetivo congelado con la fricción vendedora actual.

    La compra ya está fijada: ni la cantidad ni su comisión se recalculan como si
    se abriera otra posición al precio o ATR de este minuto.
    """
    config = AnalysisConfig.model_validate(opportunity["config"])
    original_costs = opportunity["costs"]
    quantity = float(opportunity["quantity"])
    spent = float(opportunity["reference_eur"])
    target = float(opportunity["target"])
    fee_rate = float(original_costs.get("fee_pct", config.taker_fee_pct)) / 100
    entry_fee = float(original_costs.get("entry_fee_eur", spent * fee_rate))
    result: dict[str, Any] = {
        "eligible": False,
        "reasons": [],
        "entry": opportunity["entry"],
        "stop": opportunity["stop"],
        "target": target,
        "quantity": quantity,
        "reference_eur": spent,
        "entry_fee_eur": entry_fee,
        "fee_pct": fee_rate * 100,
        "target_net_pct": None,
        "exit_price_factor": None,
        "exit_estimate": None,
        "spread_pct": None,
        "sell_slippage_pct": None,
        "cost_model": "frozen_target_current_exit_projection_v1",
        **{key: book.get(key) for key in ("ts", "observed_at", "timestamp_source")},
    }
    reasons: list[str] = result["reasons"]
    bids, asks = _levels(book.get("bids"), reverse=True), _levels(book.get("asks"), reverse=False)
    if bids is None or asks is None or asks[0][0] < bids[0][0]:
        reasons.append("Libro vacío, cruzado o con niveles inválidos")
        return result
    remaining, proceeds = quantity, 0.0
    for price, available in bids:
        sold = min(remaining, available)
        proceeds += sold * price
        remaining -= sold
        if remaining <= quantity * 1e-12:
            break
    if remaining > quantity * 1e-12:
        reasons.append("Profundidad insuficiente para vender la cantidad original")
        return result
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2
    spread = (best_ask - best_bid) / mid * 100
    exit_estimate = proceeds / quantity
    slippage = max(0.0, (1 - exit_estimate / best_bid) * 100)
    exit_factor = exit_estimate / mid
    target_net = (
        (quantity * target * exit_factor * (1 - fee_rate) - spent - entry_fee) / spent * 100
    )
    result.update(
        spread_pct=spread,
        sell_slippage_pct=slippage,
        exit_estimate=exit_estimate,
        exit_price_factor=exit_factor,
        target_net_pct=target_net,
    )
    if spread > config.max_spread_pct:
        reasons.append("Spread superior al límite")
    if slippage > config.max_slippage_pct + 1e-10:
        reasons.append("Deslizamiento de venta superior al límite")
    if target_net <= 0:
        reasons.append("Objetivo original neto no positivo después de costes")
    if not all(math.isfinite(value) for value in result.values() if isinstance(value, float)):
        reasons.append("Cálculo de costes no finito")
        for key, value in list(result.items()):
            if isinstance(value, float) and not math.isfinite(value):
                result[key] = None
    result["eligible"] = not reasons
    return result
