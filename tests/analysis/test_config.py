from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from invertio.analysis.config import AnalysisConfig, load_analysis_config
from tests.conftest import REPO_ROOT


def test_analysis_defaults_and_snapshot_are_independent() -> None:
    config = AnalysisConfig()
    assert (config.venue, config.timeframe, config.region, config.quote) == (
        "revolutx",
        "1m",
        "EEA",
        "EUR",
    )
    assert config.warmup_bars == 201
    assert config.score_params.min_atr_pct == 0
    snapshot = config.model_dump(mode="json")
    assert snapshot["reference_eur"] == 100
    assert snapshot["score_params"]["weights"]["tendencia"] == 25
    with pytest.raises(ValidationError):
        config.max_markets = 3  # type: ignore[misc]


@pytest.mark.parametrize(
    "override",
    [
        {"max_markets": 21},
        {"max_markets": 0},
        {"interval_seconds": 59},
        {"selection_seconds": 60, "interval_seconds": 120},
        {"entry_score": 40, "exit_score": 40},
        {"quote": "USD"},
        {"venue": "okx"},
        {"region": "UK"},
        {"reference_eur": float("nan")},
        {"stop_atr": float("inf")},
        {"reference_eur": -1},
        {"extra": 1},
        {"score_params": {"min_atr_pct": 0.15}},
        {"score_params": {"min_atr_pct": 0, "stop_atr": float("inf")}},
    ],
)
def test_invalid_analysis_configuration_is_rejected(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        AnalysisConfig.model_validate(override)


def test_load_configuration_and_missing_file_default(tmp_path: Path) -> None:
    assert load_analysis_config(tmp_path) == AnalysisConfig()
    (tmp_path / "analysis.yaml").write_text("max_markets: 3\nreference_eur: 75\n")
    assert load_analysis_config(tmp_path).max_markets == 3
    assert load_analysis_config(tmp_path).reference_eur == 75


def test_repository_configuration_loads() -> None:
    assert load_analysis_config(REPO_ROOT / "config") == AnalysisConfig()


def test_warmup_accounts_for_custom_long_indicators() -> None:
    config = AnalysisConfig.model_validate(
        {"score_params": {"min_atr_pct": 0, "atr_period": 300, "rsi_period": 400}}
    )
    assert config.warmup_bars == 401
