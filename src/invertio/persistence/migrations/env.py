"""Entorno de Alembic. La URL la pone `invertio db upgrade`; si se usa el CLI de alembic
directamente (alembic.ini), se toma de los settings (.env)."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from invertio.config.settings import Settings
from invertio.persistence.db import install_sqlite_pragmas
from invertio.persistence.models import Base

config = context.config
target_metadata = Base.metadata


def _db_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings.db_url()


def run_migrations_offline() -> None:
    context.configure(
        url=_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_db_url())
    install_sqlite_pragmas(engine)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite no soporta ALTER TABLE completo
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
