"""Ejecuta la misma estrategia sobre varios mercados para compararlos."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from invertio.backtest.engine import run_backtest
from invertio.backtest.metrics import compute_metrics
from invertio.backtest.report import MarketReport
from invertio.config.app_config import AppConfig
from invertio.data.download import Market, history_source
from invertio.data.store import BarStore
from invertio.strategies import load_strategy


async def backtest_markets(
    config: AppConfig,
    config_dir: Path,
    store: BarStore,
    strategy_id: str,
    markets: list[Market],
    timeframe: str,
    start: datetime,
    end: datetime,
    capital: Decimal,
) -> list[MarketReport]:
    reports = []
    for market in markets:
        source = history_source(config, market.venue)
        bars = store.read(
            source, market.symbol, timeframe, venue=market.venue.id, start=start, end=end
        )
        if not bars:
            raise ValueError(
                f"No hay velas de {market.label} entre {start:%Y-%m-%d} y {end:%Y-%m-%d}"
            )
        strategy = load_strategy(strategy_id, config_dir)  # instancia nueva por mercado
        result = await run_backtest(config, strategy, bars, capital)
        reports.append(
            MarketReport(
                result=result,
                metrics=compute_metrics(result),
                data_source=source,
                quote_currency=market.venue.quote_currency,
            )
        )
    return reports
