from decimal import Decimal

import pytest
from pydantic import ValidationError

from invertio.config.live_config import LiveConfig, LiveMarket, load_live_config
from tests.conftest import REPO_ROOT
from tests.helpers import repo_config


def test_repo_live_config_is_valid() -> None:
    live = load_live_config(REPO_ROOT / "config", repo_config())
    assert {m.venue for m in live.markets} <= set(live.initial_cash)
    assert all(m.strategy for m in live.markets)


def test_rejects_unknown_symbol_and_missing_cash() -> None:
    config = repo_config()
    bad_symbol = LiveConfig(
        initial_cash={"revolutx": Decimal(100)},
        markets=[LiveMarket(market="revolutx:DOGE/EUR", strategy="ema_cross")],
    )
    with pytest.raises(ValueError, match="DOGE/EUR"):
        bad_symbol.validate_against(config)
    no_cash = LiveConfig(
        initial_cash={},
        markets=[LiveMarket(market="revolutx:BTC/EUR", strategy="ema_cross")],
    )
    with pytest.raises(ValueError, match="initial_cash"):
        no_cash.validate_against(config)


def test_one_strategy_per_market() -> None:
    with pytest.raises(ValidationError, match="una estrategia"):
        LiveConfig(
            initial_cash={"revolutx": Decimal(100)},
            markets=[
                LiveMarket(market="revolutx:BTC/EUR", strategy="ema_cross"),
                LiveMarket(market="revolutx:BTC/EUR", strategy="rsi_reversion"),
            ],
        )
