from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from invertio.analysis.app import analysis_lock, run_analysis
from invertio.analysis.config import AnalysisConfig
from invertio.analysis.repository import AnalysisRepository
from invertio.api.app import create_app
from invertio.api.context import ApiContext
from invertio.config import Settings
from invertio.data.store import BarStore
from invertio.persistence.db import create_engine_async, session_factory, upgrade_db
from tests.helpers import repo_config


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, AnalysisRepository]]:
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    upgrade_db(settings)
    db = create_engine_async(settings)
    sessions = session_factory(db)
    ctx = ApiContext(settings, repo_config(), None, sessions, BarStore(settings.data_dir / "bars"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(ctx)), base_url="http://localhost"
    ) as client:
        yield client, AnalysisRepository(sessions)
    await db.dispose()


def opportunity(t0: datetime, ident: str = "example", **overrides: Any) -> dict[str, Any]:
    return {
        "id": ident,
        "market": "revolutx:BTC/EUR",
        "created_at": t0.isoformat(),
        "updated_at": t0.isoformat(),
        "status": "closed",
        "quality": "complete",
        "outcome": "target",
        "estimated_return_pct": 1.2,
        "rule_version": "v1",
        "ended_at": (t0 + timedelta(minutes=10)).isoformat(),
        "horizons": {},
        **overrides,
    }


async def test_empty_analysis_and_export(api: tuple[httpx.AsyncClient, AnalysisRepository]) -> None:
    client, _ = api
    status = (await client.get("/api/analysis/status")).json()
    assert status["available"] is False and status["running"] is False
    assert (await client.get("/api/analysis/ranking")).json()["rows"] == []
    summary = (await client.get("/api/analysis/summary")).json()
    assert summary["total"] == 0 and summary["mean_return_pct"] is None
    response = await client.get("/api/analysis/export.csv")
    assert (
        response.status_code == 200
        and "oportunidades-hipoteticas" in response.headers["content-disposition"]
    )
    assert (await client.get("/api/analysis/opportunities/missing")).status_code == 404


async def test_period_versions_and_unknown_returns(
    api: tuple[httpx.AsyncClient, AnalysisRepository],
    t0: datetime,
) -> None:
    client, repo = api
    rows = [
        opportunity(t0),
        opportunity(
            t0, "ambiguous", quality="ambiguous", estimated_return_pct=None, outcome="ambiguous"
        ),
        opportunity(
            t0,
            "interrupted",
            status="interrupted",
            quality="incomplete",
            estimated_return_pct=None,
            outcome="interrupted",
            rule_version="v2",
        ),
        opportunity(t0 - timedelta(days=3), "old", estimated_return_pct=-50),
    ]
    await repo.commit_cycle(
        observations=[],
        opportunities=rows,
        notifications=[],
        states={
            "service": {
                "cycles": 10,
                "rows": [],
                "last_cycle_at": t0.isoformat(),
                "config": AnalysisConfig().model_dump(mode="json"),
            }
        },
    )
    params = {
        "since": (t0 - timedelta(days=1)).isoformat(),
        "until": (t0 + timedelta(days=1)).isoformat(),
    }
    summary = (await client.get("/api/analysis/summary", params=params)).json()
    assert summary["total"] == 3 and summary["known_results"] == 1
    assert summary["mean_return_pct"] == 1.2 and summary["positive"] == 1
    assert summary["by_version"]["v2"]["mean_return_pct"] is None
    assert (await client.get("/api/analysis/status")).json()["available"] is True
    assert len((await client.get("/api/analysis/opportunities", params=params)).json()) == 3
    exported = (await client.get("/api/analysis/export.csv", params=params)).text
    assert "example" in exported and "old," not in exported


async def test_detail_filters_before_limit(
    api: tuple[httpx.AsyncClient, AnalysisRepository],
    t0: datetime,
) -> None:
    client, repo = api
    row = opportunity(t0)
    observations = [
        {
            "market": row["market"],
            "bar_ts": (t0 + timedelta(minutes=i)).isoformat(),
            "observed_at": (t0 + timedelta(minutes=i)).isoformat(),
            "rule_version": "v1",
        }
        for i in range(1100)
    ]
    await repo.commit_cycle(
        observations=observations, opportunities=[row], states={}, notifications=[]
    )
    detail = (await client.get("/api/analysis/opportunities/example")).json()
    assert len(detail["observations"]) == 11
    assert detail["notifications"] == []


async def test_bad_windows_and_csv_formula_safety(
    api: tuple[httpx.AsyncClient, AnalysisRepository],
    t0: datetime,
) -> None:
    client, repo = api
    assert (await client.get("/api/analysis/summary?since=2026-01-01T00:00:00")).status_code == 422
    assert (
        await client.get(
            "/api/analysis/summary",
            params={"since": t0.isoformat(), "until": (t0 - timedelta(days=1)).isoformat()},
        )
    ).status_code == 422
    await repo.commit_cycle(
        observations=[], opportunities=[opportunity(t0, reason="=1+2")], states={}, notifications=[]
    )
    assert "'=1+2" in (await client.get("/api/analysis/export.csv")).text


def test_only_one_analysis_writer(tmp_path: Path) -> None:
    with (
        analysis_lock(tmp_path),
        pytest.raises(ValueError, match="Ya hay un análisis"),
        analysis_lock(tmp_path),
    ):
        pass
    with analysis_lock(tmp_path):
        pass


async def test_busy_panel_aborts_before_database_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path / "untouched")
    monkeypatch.setattr("invertio.analysis.app.port_available", lambda _port: False)
    with pytest.raises(ValueError, match="ocupado"):
        await run_analysis(settings, repo_config(), AnalysisConfig())
    assert not settings.data_dir.exists()
