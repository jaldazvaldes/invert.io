from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from invertio.analysis.notifications import NotificationDispatcher, format_end, format_start
from invertio.analysis.repository import AnalysisRepository
from invertio.config.settings import Settings
from invertio.persistence.db import create_engine_async, session_factory, upgrade_db


@pytest.fixture
async def repository(tmp_path: Path) -> AsyncIterator[AnalysisRepository]:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    upgrade_db(settings)
    engine = create_engine_async(settings)
    try:
        yield AnalysisRepository(session_factory(engine))
    finally:
        await engine.dispose()


async def enqueue(repo: AnalysisRepository, t0: datetime, *, end: bool = False) -> None:
    kinds = ["start", "end"] if end else ["start"]
    await repo.commit_cycle(
        observations=[],
        states={},
        opportunities=[
            {
                "id": "opp1",
                "market": "revolutx:BTC/EUR",
                "created_at": t0.isoformat(),
                "status": "closed" if end else "open",
                "updated_at": t0.isoformat(),
            }
        ],
        notifications=[
            {
                "id": f"opp1:{kind}",
                "opportunity_id": "opp1",
                "kind": kind,
                "created_at": t0.isoformat(),
                "text": kind,
                "expires_at": (t0 + timedelta(minutes=1)).isoformat() if kind == "start" else None,
            }
            for kind in kinds
        ],
    )


async def test_ack_persists_and_restart_does_not_duplicate(
    repository: AnalysisRepository,
    t0: datetime,
) -> None:
    await enqueue(repository, t0, end=True)
    calls: list[str] = []

    async def sender(text: str) -> None:
        rows = await repository.notifications("opp1")
        row = next(row for row in rows if row["kind"] == text)
        assert row["status"] == "pending"  # not marked sent before actual ACK
        if text == "end":
            assert next(row for row in rows if row["kind"] == "start")["status"] == "sent"
        calls.append(text)

    dispatcher = NotificationDispatcher(repository, sender, lambda: t0, send_interval=0)
    await asyncio.gather(dispatcher.flush(), dispatcher.flush())
    await enqueue(repository, t0, end=True)  # restart replay cannot reset delivery state
    restarted = NotificationDispatcher(repository, sender, lambda: t0, send_interval=0)
    await restarted.flush()
    assert calls == ["start", "end"]
    assert all(row["attempts"] == 1 for row in await repository.notifications("opp1"))


async def test_failed_start_retries_before_end_and_error_never_leaks_token(
    repository: AnalysisRepository,
    t0: datetime,
) -> None:
    await enqueue(repository, t0, end=True)
    calls: list[str] = []
    fail = True

    async def sender(text: str) -> None:
        calls.append(text)
        if fail:
            raise RuntimeError("https://api.telegram.org/botTOP_SECRET/sendMessage")

    await NotificationDispatcher(repository, sender, lambda: t0, send_interval=0).flush()
    rows = await repository.notifications("opp1")
    assert calls == ["start"]
    assert rows[0]["status"] == "failed"
    assert rows[0]["attempts"] == 1
    assert "RuntimeError" in rows[0]["last_error"]
    assert "TOP_SECRET" not in str(rows)
    assert rows[1]["status"] == "pending"
    fail = False
    await NotificationDispatcher(repository, sender, lambda: t0, send_interval=0).flush()
    assert calls == ["start", "start", "end"]
    assert (await repository.notifications("opp1"))[0]["attempts"] == 2
    assert (await repository.notification_counts())["sent"] == 2


async def test_stale_start_and_its_end_are_expired_without_sending(
    repository: AnalysisRepository,
    t0: datetime,
) -> None:
    await enqueue(repository, t0, end=True)

    async def sender(_text: str) -> None:
        pytest.fail("Old opportunity was delivered as new")

    await NotificationDispatcher(
        repository,
        sender,
        lambda: t0 + timedelta(minutes=2),
        send_interval=0,
    ).flush()
    assert (await repository.notification_counts())["expired"] == 2
    assert all(row["attempts"] == 0 for row in await repository.notifications("opp1"))


async def test_missing_telegram_keeps_pending_without_claiming_delivery(
    repository: AnalysisRepository,
    t0: datetime,
) -> None:
    await enqueue(repository, t0)
    dispatcher = NotificationDispatcher(repository, clock=lambda: t0)
    assert not dispatcher.configured
    await dispatcher.flush()
    row = (await repository.notifications("opp1"))[0]
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["sent_at"] is None


async def test_end_without_start_is_suppressed(
    repository: AnalysisRepository, t0: datetime
) -> None:
    await repository.commit_cycle(
        observations=[],
        states={},
        opportunities=[
            {
                "id": "opp1",
                "market": "revolutx:BTC/EUR",
                "created_at": t0.isoformat(),
                "status": "interrupted",
                "updated_at": t0.isoformat(),
            }
        ],
        notifications=[
            {
                "id": "opp1:end",
                "opportunity_id": "opp1",
                "kind": "end",
                "created_at": t0.isoformat(),
                "text": "end",
            }
        ],
    )
    await NotificationDispatcher(repository, clock=lambda: t0).flush()
    assert (await repository.notifications("opp1"))[0]["status"] == "expired"


def test_messages_use_frozen_analysis_levels_and_escape_html(t0: datetime) -> None:
    opportunity = {
        "market": "revolutx:<BTC>/EUR",
        "entry": 100,
        "stop": 98,
        "target": 103,
        "reference_eur": 100,
        "score": 75,
        "points": {"trend": 20},
        "max_points": {"trend": 25},
        "positive": ["tendencia > 20"],
        "negative": ["volumen bajo"],
        "created_at": t0.isoformat(),
        "reason": "ambiguous",
        "quality": "ambiguous",
        "ended_at": t0.isoformat(),
    }
    start = format_start(opportunity)
    assert "experimental" in start
    assert "hipotético" in start
    assert "&lt;BTC&gt;" in start
    assert "trend: 20/25" in start
    assert "tendencia &gt; 20" in start
    assert "Entrada estimada: 100 EUR" in start
    assert "Stop: 98 EUR" in start
    assert "Objetivo: 103 EUR" in start
    assert "no es un porcentaje de acierto" in start
    end = format_end(opportunity)
    assert "ambiguous" in end
    assert "hipotético" in end
    assert "Entrada estimada: 100 EUR" in end
