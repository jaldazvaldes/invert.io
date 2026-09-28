"""Settings de entorno (.env): modo de trading, rutas y secretos.

Los secretos nunca se versionan: van en `.env`, que está en .gitignore.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from invertio.core.models import TradingMode

LIVE_CONFIRM_VALUE = "yes"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # `CLAVE=` vacío en .env equivale a no configurada
        extra="ignore",
    )

    trading_mode: TradingMode = TradingMode.PAPER
    # Segundo interruptor para dinero real: además de TRADING_MODE=live, LIVE_CONFIRM=yes.
    live_confirm: str = ""

    data_dir: Path = Path("data")
    config_dir: Path = Path("config")
    log_level: str = "INFO"
    panel_port: int = 8000  # el panel solo escucha en 127.0.0.1
    log_json: bool = False

    # Revolut X (cripto): clave API + ruta a la clave privada Ed25519 generada en tu equipo.
    revolutx_api_key: SecretStr | None = None
    revolutx_private_key_path: Path | None = None
    # Trading 212 (acciones): clave de la API pública (cuenta Invest).
    t212_api_key: SecretStr | None = None
    t212_api_secret: SecretStr | None = None
    # Alpaca (solo datos de mercado de acciones; basta una cuenta paper gratuita).
    alpaca_api_key: SecretStr | None = None
    alpaca_api_secret: SecretStr | None = None
    # Telegram: token del bot (BotFather) y tu chat_id; el bot ignora al resto.
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: int | None = None

    @model_validator(mode="after")
    def _live_requires_confirmation(self) -> Settings:
        if (
            self.trading_mode is TradingMode.LIVE
            and self.live_confirm.strip().lower() != LIVE_CONFIRM_VALUE
        ):
            raise ValueError(
                "TRADING_MODE=live mueve dinero real: para activarlo añade también "
                f"LIVE_CONFIRM={LIVE_CONFIRM_VALUE} en .env"
            )
        return self

    @property
    def db_path(self) -> Path:
        return self.data_dir / "invertio.db"

    def db_url(self, *, use_async: bool = False) -> str:
        driver = "sqlite+aiosqlite" if use_async else "sqlite"
        return f"{driver}:///{self.db_path.as_posix()}"

    def secrets_status(self) -> dict[str, bool]:
        """Qué credenciales están configuradas, sin revelar su valor."""
        return {
            "revolutx": self.revolutx_api_key is not None
            and self.revolutx_private_key_path is not None,
            "trading212": self.t212_api_key is not None,
            "alpaca": self.alpaca_api_key is not None and self.alpaca_api_secret is not None,
            "telegram": self.telegram_bot_token is not None and self.telegram_chat_id is not None,
        }
