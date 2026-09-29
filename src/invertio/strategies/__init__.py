"""Estrategias disponibles y carga de sus parámetros desde config/strategies/<id>.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from invertio.config.app_config import read_yaml
from invertio.strategies.base import Strategy, StrategyContext, StrategyParams
from invertio.strategies.breakout import BreakoutStrategy
from invertio.strategies.ema_cross import EmaCross
from invertio.strategies.pullback import PullbackStrategy
from invertio.strategies.rsi_reversion import RsiReversion
from invertio.strategies.score import ScoreStrategy
from invertio.strategies.squeeze import SqueezeStrategy
from invertio.strategies.trailing_breakout import TrailingBreakoutStrategy
from invertio.strategies.trend import TrendStrategy

STRATEGIES: dict[str, type[Strategy[Any]]] = {
    s.id: s
    for s in (
        EmaCross, RsiReversion, ScoreStrategy, TrendStrategy, BreakoutStrategy,
        TrailingBreakoutStrategy, SqueezeStrategy, PullbackStrategy,
    )
}  # fmt: skip


def load_strategy(strategy_id: str, config_dir: Path) -> Strategy[Any]:
    try:
        cls = STRATEGIES[strategy_id]
    except KeyError:
        raise ValueError(
            f"Estrategia desconocida {strategy_id!r}. Disponibles: {', '.join(sorted(STRATEGIES))}"
        ) from None
    path = config_dir / "strategies" / f"{strategy_id}.yaml"
    params = read_yaml(path) if path.exists() else {}
    return cls(cls.params_model.model_validate(params))


__all__ = ["STRATEGIES", "Strategy", "StrategyContext", "StrategyParams", "load_strategy"]
