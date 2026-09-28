"""Escáner: nota actual de la estrategia de puntuación en muchos mercados a la vez."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import structlog

from invertio.config.app_config import AppConfig
from invertio.config.settings import Settings
from invertio.core.timeframes import timeframe_delta
from invertio.live.feeds import AlpacaLiveFeed, CcxtLiveFeed, LiveFeed
from invertio.strategies.score import ScoreParams, ScoreResult, ScoreStrategy

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ScanRow:
    symbol: str
    result: ScoreResult | None
    note: str = ""  # por qué no hay nota (historial insuficiente, error…)

    def signal(self, params: ScoreParams) -> bool:
        return (
            self.result is not None
            and self.result.tradable
            and self.result.score >= params.entry_score
        )


async def scan(
    feed: LiveFeed,
    symbols: list[str],
    timeframe: str,
    params: ScoreParams,
    now: datetime,
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[ScanRow]:
    """Calcula la nota más reciente de cada símbolo con velas cerradas. Ordena de mayor a menor."""
    strategy = ScoreStrategy(params)
    since = now - (strategy.warmup_bars + 30) * timeframe_delta(timeframe)
    rows = []
    for index, symbol in enumerate(symbols, start=1):
        try:
            bars = await feed.closed_bars(symbol, timeframe, since, now)
        except Exception as exc:
            log.warning("no se pudo escanear", symbol=symbol, error=str(exc))
            rows.append(ScanRow(symbol, None, f"error: {type(exc).__name__}"))
            continue
        result = None
        for bar in bars:
            result = strategy.evaluate(bar) or result
        if result is None:
            rows.append(ScanRow(symbol, None, f"historial insuficiente ({len(bars)} velas)"))
        else:
            rows.append(ScanRow(symbol, result))
        if on_progress is not None:
            on_progress(index, len(symbols))
    return sorted(rows, key=lambda r: r.result.score if r.result else -1, reverse=True)


@dataclass(frozen=True, slots=True)
class VenueScan:
    venue: str
    rows: list[ScanRow]
    skipped: str = ""  # motivo si no se pudo escanear el venue


async def scan_venues(
    settings: Settings,
    config: AppConfig,
    timeframe: str,
    params: ScoreParams,
    now: datetime,
    *,
    all_markets: bool = False,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> list[VenueScan]:
    """Escanea cada venue activo: cripto con datos públicos, acciones si hay claves de Alpaca."""
    results = []
    for venue in config.venues:
        if not venue.enabled:
            continue
        feed: LiveFeed
        if venue.kind == "ccxt":
            ccxt_feed = CcxtLiveFeed(venue.id, venue.id)
            feed = ccxt_feed
            try:
                symbols = (
                    await ccxt_feed.symbols(venue.quote_currency) if all_markets else venue.symbols
                )
            except Exception as exc:
                await feed.close()
                results.append(VenueScan(venue.id, [], f"error: {type(exc).__name__}"))
                continue
        elif settings.alpaca_api_key is not None and settings.alpaca_api_secret is not None:
            feed = AlpacaLiveFeed(
                venue.id,
                settings.alpaca_api_key.get_secret_value(),
                settings.alpaca_api_secret.get_secret_value(),
            )
            symbols = venue.symbols
        else:
            results.append(VenueScan(venue.id, [], "sin claves de Alpaca"))
            continue
        try:

            def progress(done: int, total: int, v: str = venue.id) -> None:
                if on_progress is not None:
                    on_progress(v, done, total)

            rows = await scan(feed, symbols, timeframe, params, now, on_progress=progress)
            results.append(VenueScan(venue.id, rows))
        finally:
            await feed.close()
    return results
