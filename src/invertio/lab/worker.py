"""Trabajo de cada proceso del laboratorio: un backtest por tarea.

Se ejecuta en procesos separados (ProcessPoolExecutor), así que todo lo que usa se inicializa
con `init` y se carga bajo demanda con caché: las tareas llegan ordenadas por moneda para que
cada proceso reutilice las velas ya leídas.
"""

from __future__ import annotations

import asyncio
import bisect
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from invertio.backtest.engine import run_backtest
from invertio.backtest.metrics import compute_metrics
from invertio.config.app_config import AppConfig
from invertio.core.models import Bar
from invertio.data.store import BarStore
from invertio.lab.config import split_filter
from invertio.lab.data import resample
from invertio.logs import configure_logging
from invertio.strategies import STRATEGIES
from invertio.strategies.base import Strategy
from invertio.strategies.regime import Regime, RegimeFiltered, btc_regime

BTC_SYMBOL = "BTC/EUR"


@dataclass(frozen=True, slots=True)
class LabTask:
    strategy_id: str
    timeframe: str
    params: tuple[tuple[str, Any], ...]  # tupla ordenada: se puede usar como clave
    symbol: str
    period: str  # "train" | "test"
    start: datetime  # velas desde aquí (incluye el calentamiento)
    trade_from: datetime  # se opera y se mide desde aquí
    end: datetime


@dataclass(slots=True)
class _Context:
    config: AppConfig
    store: BarStore
    source: str
    venue: str
    base_timeframe: str
    capital: Decimal


_CTX: _Context | None = None


def init(
    config: AppConfig,
    bars_root: Path,
    source: str,
    venue: str,
    base_timeframe: str,
    capital: Decimal,
) -> None:
    global _CTX
    configure_logging("WARNING")
    _CTX = _Context(config, BarStore(bars_root), source, venue, base_timeframe, capital)
    _bars.cache_clear()
    _btc_regime.cache_clear()


@lru_cache(maxsize=6)
def _bars(symbol: str, timeframe: str) -> tuple[tuple[Bar, ...], tuple[datetime, ...]]:
    assert _CTX is not None
    base = _CTX.store.read(_CTX.source, symbol, _CTX.base_timeframe, venue=_CTX.venue)
    bars = base if timeframe == _CTX.base_timeframe else resample(base, timeframe)
    return tuple(bars), tuple(b.open_time for b in bars)


@lru_cache(maxsize=8)
def _btc_regime(days: int) -> Regime:
    """Filtro de BTC con toda su historia: no depende de la ventana de cada backtest."""
    assert _CTX is not None
    base = _CTX.store.read(_CTX.source, BTC_SYMBOL, _CTX.base_timeframe, venue=_CTX.venue)
    return btc_regime(resample(base, "1d"), days)


def run_task(task: LabTask) -> dict[str, Any]:
    assert _CTX is not None, "init() no se ha llamado"
    bars, times = _bars(task.symbol, task.timeframe)
    window = list(bars[bisect.bisect_left(times, task.start) : bisect.bisect_left(times, task.end)])
    base = {
        "strategy": task.strategy_id,
        "timeframe": task.timeframe,
        "params": dict(task.params),
        "symbol": task.symbol,
        "period": task.period,
    }
    if not window or window[-1].close_time <= task.trade_from:
        return {**base, "error": "sin velas en el periodo"}
    cls = STRATEGIES[task.strategy_id]
    params, btc_days = split_filter(dict(task.params))
    strategy: Strategy[Any] = cls(cls.params_model.model_validate(params))
    if btc_days is not None:
        strategy = RegimeFiltered(strategy, _btc_regime(btc_days))
    try:
        result = asyncio.run(
            run_backtest(_CTX.config, strategy, window, _CTX.capital, trade_from=task.trade_from)
        )
    except Exception as exc:  # una prueba rota no debe tumbar las demás
        return {**base, "error": f"{type(exc).__name__}: {exc}"[:200]}
    if len(result.equity_curve) < 2:
        return {**base, "error": "periodo demasiado corto"}
    metrics = compute_metrics(result)
    return {**base, **metrics.as_dict()}
