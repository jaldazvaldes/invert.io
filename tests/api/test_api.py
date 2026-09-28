import json
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.testclient import TestClient

from invertio.api.app import create_app
from invertio.api.context import ApiContext
from invertio.api.hub import EventHub
from invertio.config import Settings
from invertio.core.clock import SimClock
from invertio.data.store import BarStore
from invertio.live.engine import LiveEngine
from invertio.persistence.db import create_engine_async, session_factory, upgrade_db
from tests.helpers import make_bars, repo_config
from tests.live.test_live_engine import (
    AtTimes,
    FakeFeed,
    RecordingNotifier,
    _advance,
    _after_close,
    _engine,
    _live_config,
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    upgrade_db(settings)
    return settings


@pytest.fixture
async def sessions(settings: Settings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine_async(settings)
    yield session_factory(engine)
    await engine.dispose()


def _context(
    settings: Settings, sessions: async_sessionmaker[AsyncSession], engine: LiveEngine | None
) -> ApiContext:
    return ApiContext(
        settings=settings,
        config=repo_config(prefer_post_only=False),
        live=_live_config(),
        sessions=sessions,
        store=BarStore(settings.data_dir / "bars"),
        engine=engine,
    )


def _client(ctx: ApiContext, hub: EventHub | None = None) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(ctx, hub))
    return httpx.AsyncClient(transport=transport, base_url="http://localhost")


async def test_security_rules(
    settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> None:
    ctx = _context(settings, sessions, engine=None)
    async with _client(ctx) as client:
        evil = await client.get("/api/status", headers={"host": "evil.example.com"})
        assert evil.status_code == 400  # DNS rebinding
        assert (await client.post("/api/control/pause")).status_code == 403  # sin token
        headers = {"X-Invertio-Token": (await client.get("/api/session")).json()["token"]}
        response = await client.post("/api/control/pause", headers=headers)
        assert response.status_code == 503  # motor parado
        status = (await client.get("/api/status")).json()
        assert status["running"] is False and status["state"] is None
        assert [m["market"] for m in status["markets"]] == ["revolutx:BTC/EUR"]


async def test_live_status_controls_bars_and_activity(
    t0: datetime, settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> None:
    bars = make_bars([100.0] * 12 + [101, 102, 103, 104, 105], t0)
    clock = SimClock(_after_close(t0, 10))
    engine = _engine(
        sessions,
        clock,
        FakeFeed("revolutx", bars),
        AtTimes(bars[11].open_time),
        RecordingNotifier(),
    )
    engine.store = BarStore(settings.data_dir / "bars")
    await engine.start()
    for n in (11, 12, 13, 14):
        await _advance(engine, clock, t0, n)
    await engine.recorder.flush()

    ctx = _context(settings, sessions, engine)
    ctx.engine = engine
    async with _client(ctx) as client:
        headers = {"X-Invertio-Token": ctx.token}
        status = (await client.get("/api/status")).json()
        assert status["running"] is True and status["state"] == "running"
        [venue] = status["venues"]
        [position] = venue["positions"]
        assert position["symbol"] == "BTC/EUR" and position["stop_loss"] is not None
        assert status["markets"][0]["enabled"] is True

        activity = (await client.get("/api/activity")).json()
        kinds = {item["kind"] for item in activity}
        assert kinds == {"signal", "fill"}
        signal = next(i for i in activity if i["kind"] == "signal")
        assert signal["approved"] is True and signal["order_status"] == "filled"

        data = (await client.get("/api/bars", params={"market": "revolutx:BTC/EUR"})).json()
        assert len(data["bars"]) >= 5 and data["tf_seconds"] == 300
        assert data["fills"][0]["side"] == "buy" and data["position"]["quantity"] > 0

        equity = (await client.get("/api/equity", params={"venue": "revolutx"})).json()
        assert equity and equity[-1]["equity"] > 0

        toggle = await client.post(
            "/api/markets/toggle",
            json={"market": "revolutx:BTC/EUR", "enabled": False},
            headers=headers,
        )
        assert toggle.status_code == 200
        status = (await client.get("/api/status")).json()
        assert status["markets"][0]["enabled"] is False

        assert (await client.post("/api/control/pause", headers=headers)).json() == {
            "state": "paused"
        }
        refused = await client.post("/api/control/panic", json={"confirm": False}, headers=headers)
        assert refused.status_code == 400
        panic = await client.post("/api/control/panic", json={"confirm": True}, headers=headers)
        assert panic.json() == {"state": "halted", "closed": 1}
        trades = (await client.get("/api/trades")).json()
        assert trades[0]["exit_reason"] == "pánico"
    await engine.shutdown()


async def test_backtests_listing(
    settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> None:
    folder = settings.data_dir / "reports" / "20260928-124811_rsi_reversion"
    folder.mkdir(parents=True)
    (folder / "summary.json").write_text(
        json.dumps(
            {
                "strategy": "rsi_reversion",
                "markets": [
                    {"market": "revolutx:BTC/EUR", "timeframe": "5m",
                     "metrics": {"total_return_pct": -10.2, "trades": 123}}
                ],
            }
        ),
        encoding="utf-8",
    )  # fmt: skip
    (folder / "report.html").write_text("<html>ok</html>", encoding="utf-8")
    async with _client(_context(settings, sessions, engine=None)) as client:
        [item] = (await client.get("/api/backtests")).json()
        assert item["strategy"] == "rsi_reversion"
        assert item["markets"][0]["total_return_pct"] == -10.2
        report = await client.get(item["report_url"])
        assert report.status_code == 200 and "ok" in report.text


def test_websocket_origin_and_broadcast(
    settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> None:
    hub = EventHub()
    app = create_app(_context(settings, sessions, engine=None), hub)
    with TestClient(app, base_url="http://localhost") as client:
        from starlette.testclient import WebSocketDenialResponse
        from starlette.websockets import WebSocketDisconnect

        with (
            pytest.raises((WebSocketDisconnect, WebSocketDenialResponse)),
            client.websocket_connect(
                "ws://localhost/api/ws", headers={"origin": "https://evil.example.com"}
            ) as ws,
        ):
            ws.receive_json()
        with client.websocket_connect(
            "ws://localhost/api/ws", headers={"origin": "http://localhost:8000"}
        ) as ws:
            assert ws.receive_json() == {"type": "hello", "running": False}
            hub.broadcast({"type": "state", "state": "paused", "reason": "prueba"})
            assert ws.receive_json()["state"] == "paused"


async def test_lab_endpoint_returns_latest_run(
    settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> None:
    async with _client(_context(settings, sessions, engine=None)) as client:
        assert (await client.get("/api/lab")).json() is None
        folder = settings.data_dir / "lab" / "20260928-150000"
        folder.mkdir(parents=True)
        candidates = [
            {"strategy": "tendencia", "timeframe": "1d", "train_rank": 1, "verdict": "no aprueba"},
            {"strategy": "tendencia", "timeframe": "1d", "train_rank": 2, "verdict": "no aprueba"},
        ]
        (folder / "summary.json").write_text(
            json.dumps({"test_start": "2025-09-28", "candidates": candidates}), encoding="utf-8"
        )
        (folder / "report.html").write_text("<html>lab</html>", encoding="utf-8")
        data = (await client.get("/api/lab")).json()
        assert data["id"] == "20260928-150000"
        assert [c["train_rank"] for c in data["candidates"]] == [1]  # solo la mejor de cada una
        assert (await client.get(data["report_url"])).text == "<html>lab</html>"
