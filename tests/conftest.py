from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def t0() -> datetime:
    return datetime(2026, 1, 5, 14, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Los tests nunca leen el .env real ni variables del sistema del usuario."""
    monkeypatch.chdir(tmp_path)
    for name in (
        "TRADING_MODE",
        "LIVE_CONFIRM",
        "DATA_DIR",
        "CONFIG_DIR",
        "REVOLUTX_API_KEY",
        "T212_API_KEY",
        "ALPACA_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
