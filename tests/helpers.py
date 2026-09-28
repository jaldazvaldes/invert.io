"""Utilidades compartidas por los tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from invertio.config import AppConfig, load_app_config
from invertio.core.models import Bar
from invertio.core.timeframes import timeframe_delta
from tests.conftest import REPO_ROOT


def repo_config(**risk_overrides: Any) -> AppConfig:
    config = load_app_config(REPO_ROOT / "config")
    if risk_overrides:
        config = config.model_copy(update={"risk": config.risk.model_copy(update=risk_overrides)})
    return config


def with_venue(config: AppConfig, venue_id: str, **updates: Any) -> AppConfig:
    """Copia de `config` con campos del venue cambiados (p. ej. simulation, fees)."""
    venues = [v.model_copy(update=updates) if v.id == venue_id else v for v in config.venues]
    return config.model_copy(update={"venues": venues})


def make_bars(
    closes: Sequence[float],
    start: datetime,
    *,
    venue: str = "revolutx",
    symbol: str = "BTC/EUR",
    timeframe: str = "5m",
    wick_pct: float = 0.1,
) -> list[Bar]:
    """Velas sintéticas: cada una abre en el cierre anterior y tiene mechas de `wick_pct` %."""
    step = timeframe_delta(timeframe)
    bars = []
    prev = closes[0]
    for i, close in enumerate(closes):
        high = max(prev, close) * (1 + wick_pct / 100)
        low = min(prev, close) * (1 - wick_pct / 100)
        bars.append(Bar(venue, symbol, timeframe, start + i * step, prev, high, low, close, 1.0))
        prev = close
    return bars


def bar(
    open_time: datetime,
    o: float,
    h: float,
    low: float,
    c: float,
    *,
    venue: str = "revolutx",
    symbol: str = "BTC/EUR",
) -> Bar:
    return Bar(venue, symbol, "5m", open_time, o, h, low, c, 1.0)


def minutes(n: int) -> timedelta:
    return timedelta(minutes=n)
