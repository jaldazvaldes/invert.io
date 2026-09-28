from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy.exc import IntegrityError

from invertio.analysis.repository import AnalysisRepository
from invertio.config.settings import Settings
from invertio.persistence.db import (
    alembic_config,
    create_engine_async,
    session_factory,
    upgrade_db,
)


@pytest.fixture
async def repository(tmp_path: Path) -> AsyncIterator[AnalysisRepository]:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    upgrade_db(settings)
    engine = create_engine_async(settings)
    try:
        yield AnalysisRepository(session_factory(engine))
    finally:
        await engine.dispose()


def opportunity(t0: datetime, *, ident: str = "opp1", status: str = "open") -> dict:
    return {
        "id": ident,
        "market": "revolutx:BTC/EUR",
        "created_at": t0.isoformat(),
        "updated_at": t0.isoformat(),
        "status": status,
        "entry": 100.1,
        "config": {"taker_fee": 0.0009},
        "positive": ["volumen"],
    }


def notification(t0: datetime) -> dict:
    return {
        "id": "opp1:start",
        "opportunity_id": "opp1",
        "kind": "start",
        "created_at": t0.isoformat(),
        "text": "experimental",
        "expires_at": (t0 + timedelta(minutes=1)).isoformat(),
    }


async def test_snapshots_upsert_exact_payload_and_outbox_is_idempotent(
    repository: AnalysisRepository,
    t0: datetime,
) -> None:
    obs = {
        "market": "revolutx:BTC/EUR",
        "bar_ts": t0.isoformat(),
        "observed_at": t0.isoformat(),
        "rule_version": "v1",
        "score": 75,
        "factors": {"momentum": 12},
    }
    opp = opportunity(t0)
    state = {"revolutx:BTC/EUR": {"armed": False, "last_bar": t0.isoformat()}}
    await repository.commit_cycle(
        observations=[obs],
        opportunities=[opp],
        states=state,
        notifications=[notification(t0)],
    )
    await repository.mark_notification("opp1:start", "sent", now=t0)
    await repository.commit_cycle(
        observations=[obs],
        opportunities=[opp],
        states=state,
        notifications=[notification(t0)],
    )
    assert await repository.observations() == [obs]
    assert await repository.opportunities() == [opp]
    assert await repository.opportunity("opp1") == opp
    assert await repository.opportunity("missing") is None
    assert await repository.load_state() == state
    delivered = await repository.notifications("opp1")
    assert len(delivered) == 1
    assert delivered[0]["status"] == "sent"
    assert delivered[0]["attempts"] == 1
    assert delivered[0]["sent_at"] == t0.isoformat()
    assert await repository.notification_counts() == {
        "pending": 0,
        "sent": 1,
        "failed": 0,
        "expired": 0,
    }


async def test_filtering_update_and_reopening_in_same_cycle(
    repository: AnalysisRepository,
    t0: datetime,
) -> None:
    original = opportunity(t0)
    await repository.commit_cycle(
        observations=[],
        opportunities=[original],
        states={},
        notifications=[],
    )
    closed = {**original, "status": "closed", "reason": "target"}
    new = opportunity(t0 + timedelta(minutes=5), ident="opp2")
    await repository.commit_cycle(
        observations=[],
        opportunities=[new, closed],
        states={},
        notifications=[],
    )
    assert await repository.opportunities() == [new, closed]
    assert await repository.opportunities(status="closed") == [closed]
    assert await repository.opportunities(since=t0 + timedelta(minutes=1)) == [new]
    assert await repository.opportunities(until=t0) == [closed]


async def test_observation_key_scopes_exchange_bar_and_rule(
    repository: AnalysisRepository,
    t0: datetime,
) -> None:
    obs = {
        "market": "revolutx:BTC/EUR",
        "bar_ts": t0.isoformat(),
        "observed_at": t0.isoformat(),
        "rule_version": "v1",
        "score": 75,
    }
    await repository.commit_cycle(
        observations=[obs, {**obs, "market": "other:BTC/EUR"}, {**obs, "rule_version": "v2"}],
        opportunities=[],
        states={},
        notifications=[],
    )
    updated = {**obs, "score": 77, "observed_at": (t0 + timedelta(seconds=30)).isoformat()}
    await repository.commit_cycle(
        observations=[updated],
        opportunities=[],
        states={},
        notifications=[],
    )
    assert len(await repository.observations()) == 3
    assert len(await repository.observations(market=obs["market"])) == 2
    assert await repository.observations(limit=1) == [updated]
    assert await repository.observations(limit=0) == []


async def test_cycle_rolls_back_if_outbox_or_unique_opportunity_fails(
    repository: AnalysisRepository,
    t0: datetime,
) -> None:
    with pytest.raises(ValueError, match="opportunity_id:kind"):
        await repository.commit_cycle(
            observations=[],
            opportunities=[opportunity(t0)],
            states={"test": {"written": True}},
            notifications=[{**notification(t0), "id": "non-deterministic"}],
        )
    assert await repository.opportunities() == []
    assert await repository.load_state() == {}
    with pytest.raises(IntegrityError):
        await repository.commit_cycle(
            observations=[],
            opportunities=[opportunity(t0), opportunity(t0, ident="opp2")],
            states={},
            notifications=[],
        )
    assert await repository.opportunities() == []


async def test_naive_timestamp_does_not_silently_enter_history(
    repository: AnalysisRepository,
    t0: datetime,
) -> None:
    with pytest.raises(ValueError, match="timezone"):
        await repository.commit_cycle(
            observations=[],
            opportunities=[{**opportunity(t0), "created_at": "2026-01-01T12:00:00"}],
            states={},
            notifications=[],
        )


def test_migration_and_downgrade_preserve_existing_paper_data(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    upgrade_db(settings, "03d7ee18ac9b")
    with sqlite3.connect(settings.db_path) as connection:
        connection.execute(
            "INSERT INTO audit_log(mode,ts,level,event,data) VALUES(?,?,?,?,?)",
            ("paper", "2026-01-01 12:00:00", "info", "existing", '{"keep":true}'),
        )
    upgrade_db(settings)
    upgrade_db(settings)
    with sqlite3.connect(settings.db_path) as connection:
        rows = connection.execute("SELECT mode,event,data FROM audit_log").fetchall()
        assert rows == [("paper", "existing", '{"keep":true}')]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        assert {
            "analysis_observations",
            "analysis_opportunities",
            "analysis_state",
            "analysis_notifications",
            "orders",
            "fills",
            "signals",
        } <= tables
    command.downgrade(alembic_config(settings.db_url()), "03d7ee18ac9b")
    with sqlite3.connect(settings.db_path) as connection:
        assert connection.execute("SELECT mode,event,data FROM audit_log").fetchall() == rows
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        assert "analysis_opportunities" not in tables
        assert {"orders", "fills", "signals", "equity_snapshots", "audit_log"} <= tables
