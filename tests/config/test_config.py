from pathlib import Path

import pytest
from pydantic import ValidationError

from invertio.config import Settings, load_app_config
from invertio.core.models import AssetClass, TradingMode
from tests.conftest import REPO_ROOT


class TestSettings:
    def test_defaults_to_paper(self) -> None:
        settings = Settings(_env_file=None)
        assert settings.trading_mode is TradingMode.PAPER
        assert not any(settings.secrets_status().values())

    def test_live_requires_explicit_confirmation(self) -> None:
        with pytest.raises(ValidationError, match="LIVE_CONFIRM"):
            Settings(_env_file=None, trading_mode=TradingMode.LIVE)
        with pytest.raises(ValidationError, match="LIVE_CONFIRM"):
            Settings(_env_file=None, trading_mode=TradingMode.LIVE, live_confirm="si")
        settings = Settings(_env_file=None, trading_mode=TradingMode.LIVE, live_confirm="YES")
        assert settings.trading_mode is TradingMode.LIVE

    def test_reads_env_file_and_ignores_empty_values(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("TRADING_MODE=demo\nTELEGRAM_CHAT_ID=\nT212_API_KEY=abc\n", encoding="utf-8")
        settings = Settings(_env_file=env)
        assert settings.trading_mode is TradingMode.DEMO
        assert settings.telegram_chat_id is None
        assert settings.secrets_status()["trading212"] is True
        assert "abc" not in repr(settings)  # los secretos no se imprimen

    def test_db_urls(self, tmp_path: Path) -> None:
        settings = Settings(_env_file=None, data_dir=tmp_path)
        assert settings.db_url().startswith("sqlite:///")
        assert settings.db_url(use_async=True).startswith("sqlite+aiosqlite:///")


class TestAppConfig:
    def test_repo_config_is_valid(self) -> None:
        config = load_app_config(REPO_ROOT / "config")
        assert config.venue("revolutx").asset_class is AssetClass.CRYPTO
        assert config.venue("trading212").fees.fx_pct == pytest.approx(0.15)
        assert config.risk.max_open_positions >= 1

    def test_rejects_out_of_range_risk(self, tmp_path: Path) -> None:
        (tmp_path / "app.yaml").write_text(
            (REPO_ROOT / "config" / "app.yaml").read_text(encoding="utf-8"), encoding="utf-8"
        )
        risk = (REPO_ROOT / "config" / "risk.yaml").read_text(encoding="utf-8")
        (tmp_path / "risk.yaml").write_text(
            risk.replace("max_daily_loss_pct: 3", "max_daily_loss_pct: 80"), encoding="utf-8"
        )
        with pytest.raises(ValidationError, match="max_daily_loss_pct"):
            load_app_config(tmp_path)

    def test_rejects_unknown_keys(self, tmp_path: Path) -> None:
        (tmp_path / "app.yaml").write_text("venues: []\nextra: 1\n", encoding="utf-8")
        (tmp_path / "risk.yaml").write_text(
            (REPO_ROOT / "config" / "risk.yaml").read_text(encoding="utf-8"), encoding="utf-8"
        )
        with pytest.raises(ValidationError, match="extra"):
            load_app_config(tmp_path)

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_app_config(tmp_path)
