"""Conexión a SQLite y migraciones."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from invertio.config.settings import Settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _set_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")  # lectores (panel) y escritor (motor) a la vez
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def alembic_config(db_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", db_url)
    return config


def upgrade_db(settings: Settings, revision: str = "head") -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(settings.db_url()), revision)


def install_sqlite_pragmas(engine: Engine) -> None:
    event.listen(engine, "connect", _set_sqlite_pragmas)


def create_engine_async(settings: Settings) -> AsyncEngine:
    engine = create_async_engine(settings.db_url(use_async=True))
    install_sqlite_pragmas(engine.sync_engine)
    return engine


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
