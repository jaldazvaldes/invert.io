"""API del panel web (FastAPI).

Seguridad (el panel solo escucha en 127.0.0.1):
- `TrustedHostMiddleware` rechaza cabeceras Host ajenas: protege frente a DNS rebinding.
- Las acciones (pausa, pánico, activar mercados, escanear) exigen la cabecera `X-Invertio-Token`
  con el token de sesión. Otra web abierta en el navegador no puede leerlo (misma-origen) ni
  enviar esa cabecera sin permiso CORS, que no se concede.
- El WebSocket comprueba el Origin.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select
from starlette.middleware.trustedhost import TrustedHostMiddleware

from invertio.api.context import ApiContext
from invertio.api.hub import CLOSE, EventHub
from invertio.core.models import TradingMode
from invertio.core.timeframes import timeframe_seconds
from invertio.live.report import load_paper_portfolio
from invertio.persistence.models import EquitySnapshotRow, FillRow, OrderRow, SignalRow
from invertio.portfolio import ClosedTrade, Portfolio
from invertio.strategies import load_strategy
from invertio.strategies.score import FACTORS, ScoreParams, ScoreResult, ScoreStrategy

log = structlog.get_logger(__name__)

ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"
MODE = TradingMode.PAPER.value


def _f(value: Decimal | float | None) -> float | None:
    return None if value is None else float(value)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _score(result: ScoreResult, meets: bool) -> dict[str, Any]:
    return {
        "market": f"{result.venue}:{result.symbol}",
        "venue": result.venue,
        "symbol": result.symbol,
        "ts": result.ts.isoformat(),
        "close": result.close,
        "score": result.score,
        "points": result.points,
        "max_points": result.max_points,
        "factors": list(FACTORS),
        "atr_pct": result.atr_pct,
        "tradable": result.tradable,
        "stop_loss": result.stop_loss,
        "take_profit": result.take_profit,
        "meets": meets,
    }


def _trade(t: ClosedTrade, currency: str) -> dict[str, Any]:
    return {
        "market": f"{t.venue}:{t.symbol}",
        "currency": currency,
        "strategy": t.strategy_id,
        "entry_time": t.entry_time.isoformat(),
        "exit_time": t.exit_time.isoformat(),
        "quantity": float(t.quantity),
        "entry_price": float(t.entry_price),
        "exit_price": float(t.exit_price),
        "pnl": float(t.pnl),
        "fees": float(t.fees),
        "return_pct": t.return_pct,
        "exit_reason": t.exit_reason,
    }


class ToggleMarket(BaseModel):
    market: str
    enabled: bool


class PanicRequest(BaseModel):
    confirm: bool


class ScanRequest(BaseModel):
    timeframe: str = "1h"
    all_markets: bool = True


def create_app(ctx: ApiContext, hub: EventHub | None = None) -> FastAPI:
    app = FastAPI(title="invert.io", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
    hub = hub or EventHub()

    def require_token(x_invertio_token: Annotated[str | None, Header()] = None) -> None:
        if x_invertio_token != ctx.token:
            raise HTTPException(status_code=403, detail="Token de sesión no válido")

    def require_engine() -> None:
        if ctx.engine is None:
            raise HTTPException(
                status_code=503, detail="El motor no está arrancado (usa: invertio run)"
            )

    async def portfolio() -> Portfolio | None:
        if ctx.engine is not None:
            return ctx.engine.portfolio
        if ctx.live is None:
            return None
        portfolio, _ = await load_paper_portfolio(ctx.sessions, ctx.config, ctx.live)
        return portfolio

    def last_close(venue: str, symbol: str) -> Decimal | None:
        info = ctx.store.info(venue, symbol, ctx.timeframe)
        if info is None:
            return None
        bars = ctx.store.read(venue, symbol, ctx.timeframe, venue=venue, last=1)
        return Decimal(str(bars[-1].close)) if bars else None

    # --- sesión y configuración --------------------------------------------------------

    @app.get("/api/session")
    async def session() -> dict[str, Any]:
        return {"token": ctx.token}

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        return {
            "mode": ctx.settings.trading_mode.value,
            "timeframe": ctx.timeframe,
            "risk": ctx.config.risk.model_dump(),
            "venues": [
                {
                    "id": v.id,
                    "asset_class": v.asset_class.value,
                    "quote": v.quote_currency,
                    "symbols": v.symbols,
                    "fees": v.fees.model_dump(),
                }
                for v in ctx.config.venues
            ],
            "markets": [m.model_dump() for m in ctx.live.markets] if ctx.live else [],
        }

    # --- estado ------------------------------------------------------------------------

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        engine = ctx.engine
        pf = await portfolio()
        venues = []
        if pf is not None:
            for venue_id, initial in pf.initial_cash.items():
                currency = ctx.config.venue(venue_id).quote_currency
                positions = []
                for p in pf.open_positions(venue_id):
                    last = pf.last_price(venue_id, p.symbol) or last_close(venue_id, p.symbol)
                    unrealized = p.unrealized_pnl(last) if last else Decimal(0)
                    cost = p.avg_price * p.quantity
                    positions.append(
                        {
                            "market": f"{venue_id}:{p.symbol}",
                            "symbol": p.symbol,
                            "quantity": float(p.quantity),
                            "avg_price": float(p.avg_price),
                            "last_price": _f(last),
                            "unrealized": float(unrealized),
                            "unrealized_pct": float(unrealized / cost * 100) if cost else 0.0,
                            "stop_loss": _f(p.stop_loss),
                            "take_profit": _f(p.take_profit),
                            "strategy": pf.entry_strategy(venue_id, p.symbol),
                        }
                    )
                equity = pf.equity(venue_id)
                if engine is None:  # sin motor: valorar posiciones con la última vela guardada
                    equity = pf.cash(venue_id) + sum(
                        (
                            Decimal(str(p["last_price"] or p["avg_price"]))
                            * Decimal(str(p["quantity"]))
                        )
                        for p in positions
                    )
                venues.append(
                    {
                        "venue": venue_id,
                        "currency": currency,
                        "equity": float(equity),
                        "cash": float(pf.cash(venue_id)),
                        "initial": float(initial),
                        "return_pct": float(equity / initial - 1) * 100 if initial else 0.0,
                        "positions": positions,
                    }
                )
        markets = []
        if engine is not None:
            stale = {m.key: m.stale for m in engine.markets}
            for key, strategy_id, enabled in engine.controller.markets():
                last_bar = engine.controller.last_bar.get(key)
                markets.append(
                    {
                        "market": f"{key[0]}:{key[1]}",
                        "strategy": strategy_id,
                        "enabled": enabled,
                        "last_bar": _iso(last_bar),
                        "stale": stale.get(key, False),
                    }
                )
        elif ctx.live is not None:
            markets = [
                {
                    "market": m.market,
                    "strategy": m.strategy,
                    "enabled": True,
                    "last_bar": None,
                    "stale": False,
                }
                for m in ctx.live.markets
            ]
        today = None
        if engine is not None:
            s = engine.controller.today()
            today = {
                "trades": s.trades,
                "wins": s.wins,
                "pnl": {c: float(v) for c, v in s.pnl_by_currency.items()},
                "signals": s.signals,
                "approved": s.approved,
                "rejections": s.rejections,
            }
        return {
            "running": engine is not None,
            "mode": ctx.settings.trading_mode.value,
            "state": engine.controller.state.value if engine else None,
            "timeframe": ctx.timeframe,
            "server_time": datetime.now(UTC).isoformat(),
            "venues": venues,
            "markets": markets,
            "today": today,
        }

    # --- acciones ----------------------------------------------------------------------

    @app.post("/api/control/pause", dependencies=[Depends(require_token), Depends(require_engine)])
    async def pause() -> dict[str, str]:
        assert ctx.engine is not None
        await ctx.engine.controller.pause("pedido desde el panel")
        return {"state": ctx.engine.controller.state.value}

    @app.post("/api/control/resume", dependencies=[Depends(require_token), Depends(require_engine)])
    async def resume() -> dict[str, str]:
        assert ctx.engine is not None
        await ctx.engine.controller.resume("pedido desde el panel")
        return {"state": ctx.engine.controller.state.value}

    @app.post("/api/control/panic", dependencies=[Depends(require_token), Depends(require_engine)])
    async def panic(body: PanicRequest) -> dict[str, Any]:
        assert ctx.engine is not None
        if not body.confirm:
            raise HTTPException(status_code=400, detail="Falta confirmar el pánico")
        closed = await ctx.engine.controller.panic("botón de pánico (panel)")
        return {"state": ctx.engine.controller.state.value, "closed": closed}

    @app.post("/api/markets/toggle", dependencies=[Depends(require_token), Depends(require_engine)])
    async def toggle_market(body: ToggleMarket) -> dict[str, Any]:
        assert ctx.engine is not None
        venue, _, symbol = body.market.partition(":")
        try:
            ctx.engine.controller.set_market_enabled((venue, symbol), body.enabled)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return {"market": body.market, "enabled": body.enabled}

    # --- puntuación y escáner -------------------------------------------------------------

    @app.get("/api/scores")
    async def scores() -> list[dict[str, Any]]:
        if ctx.engine is None:
            return []
        return [_score(result, meets) for result, meets in ctx.engine.controller.scores()]

    @app.get("/api/scan")
    async def scan_state() -> dict[str, Any]:
        s = ctx.scan
        return {
            "status": s.status,
            "timeframe": s.timeframe,
            "all_markets": s.all_markets,
            "progress": {venue: list(p) for venue, p in s.progress.items()},
            "started_at": _iso(s.started_at),
            "finished_at": _iso(s.finished_at),
            "rows": s.rows,
            "skipped": s.skipped,
            "error": s.error,
        }

    @app.post("/api/scan", dependencies=[Depends(require_token)])
    async def start_scan(body: ScanRequest) -> dict[str, Any]:
        from invertio.live.scanner import scan_venues

        if ctx.scan.status == "running":
            raise HTTPException(status_code=409, detail="Ya hay un escaneo en marcha")
        try:
            timeframe_seconds(body.timeframe)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        strategy = load_strategy(ScoreStrategy.id, ctx.settings.config_dir)
        assert isinstance(strategy.params, ScoreParams)
        params = strategy.params
        state = ctx.scan
        state.status, state.error = "running", None
        state.timeframe, state.all_markets = body.timeframe, body.all_markets
        state.progress, state.rows, state.skipped = {}, [], []
        state.started_at, state.finished_at = datetime.now(UTC), None

        def progress(venue: str, done: int, total: int) -> None:
            state.progress[venue] = (done, total)

        async def run() -> None:
            try:
                scans = await scan_venues(
                    ctx.settings,
                    ctx.config,
                    body.timeframe,
                    params,
                    datetime.now(UTC),
                    all_markets=body.all_markets,
                    on_progress=progress,
                )
                rows = []
                for venue_scan in scans:
                    if venue_scan.skipped:
                        state.skipped.append(
                            {"venue": venue_scan.venue, "reason": venue_scan.skipped}
                        )
                    for row in venue_scan.rows:
                        if row.result is not None:
                            rows.append(_score(row.result, row.signal(params)))
                        else:
                            rows.append(
                                {"market": f"{venue_scan.venue}:{row.symbol}", "note": row.note}
                            )
                rows.sort(key=lambda r: r.get("score", -1), reverse=True)
                state.rows, state.status = rows, "done"
            except Exception as exc:
                log.exception("error en el escaneo")
                state.status, state.error = "error", str(exc)
            finally:
                state.finished_at = datetime.now(UTC)
                hub.broadcast({"type": "scan", "status": state.status})

        state.task = asyncio.create_task(run(), name="scan")
        return {"status": "running"}

    # --- historial -----------------------------------------------------------------------

    @app.get("/api/activity")
    async def activity(limit: Annotated[int, Query(ge=1, le=500)] = 150) -> list[dict[str, Any]]:
        """Señales (con la decisión del riesgo y su orden) y ejecuciones, de más reciente a más
        antigua."""
        async with ctx.sessions() as session:
            signal_rows = (
                await session.execute(
                    select(SignalRow, OrderRow)
                    .outerjoin(OrderRow, OrderRow.signal_id == SignalRow.id)
                    .where(SignalRow.mode == MODE)
                    .order_by(SignalRow.ts.desc())
                    .limit(limit)
                )
            ).all()
            fill_rows = (
                await session.execute(
                    select(FillRow, OrderRow)
                    .join(OrderRow, OrderRow.client_order_id == FillRow.client_order_id)
                    .where(FillRow.mode == MODE)
                    .order_by(FillRow.ts.desc())
                    .limit(limit)
                )
            ).all()
        items: list[dict[str, Any]] = []
        for signal, order in signal_rows:
            items.append(
                {
                    "kind": "signal",
                    "ts": signal.ts.isoformat(),
                    "market": f"{signal.venue}:{signal.symbol}",
                    "strategy": signal.strategy_id,
                    "action": signal.action,
                    "price": signal.price,
                    "stop_loss": signal.stop_loss,
                    "take_profit": signal.take_profit,
                    "reason": signal.reason,
                    "approved": signal.approved,
                    "decision": signal.decision_reason,
                    "order_status": order.status if order else None,
                    "order_type": order.type if order else None,
                    "fill_price": _f(order.avg_fill_price) if order else None,
                }
            )
        for fill, order in fill_rows:
            items.append(
                {
                    "kind": "fill",
                    "ts": fill.ts.isoformat(),
                    "market": f"{fill.venue}:{fill.symbol}",
                    "strategy": order.strategy_id,
                    "side": fill.side,
                    "quantity": float(fill.quantity),
                    "price": float(fill.price),
                    "fee": float(fill.fee),
                    "currency": fill.fee_currency,
                    "reason": order.reason,
                }
            )
        items.sort(key=lambda item: item["ts"], reverse=True)
        return items[:limit]

    @app.get("/api/trades")
    async def trades(limit: Annotated[int, Query(ge=1, le=1000)] = 200) -> list[dict[str, Any]]:
        pf = await portfolio()
        if pf is None:
            return []
        return [
            _trade(t, ctx.config.venue(t.venue).quote_currency)
            for t in reversed(pf.closed_trades[-limit:])
        ]

    @app.get("/api/equity")
    async def equity(
        venue: str, points: Annotated[int, Query(ge=10, le=5000)] = 1500
    ) -> list[dict[str, Any]]:
        async with ctx.sessions() as session:
            rows = (
                await session.scalars(
                    select(EquitySnapshotRow)
                    .where(EquitySnapshotRow.mode == MODE, EquitySnapshotRow.venue == venue)
                    .order_by(EquitySnapshotRow.ts)
                )
            ).all()
        step = max(1, len(rows) // points)
        sampled = list(rows[::step])
        if rows and sampled[-1] is not rows[-1]:
            sampled.append(rows[-1])
        return [{"ts": r.ts.isoformat(), "equity": float(r.equity)} for r in sampled]

    @app.get("/api/bars")
    async def bars(
        market: str,
        limit: Annotated[int, Query(ge=10, le=5000)] = 300,
        tf: str | None = None,
    ) -> dict[str, Any]:
        """Velas guardadas por el modo en vivo, ejecuciones del mercado y posición abierta."""
        venue, _, symbol = market.partition(":")
        timeframe = tf or ctx.timeframe
        try:
            seconds = timeframe_seconds(timeframe)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        try:
            candles = ctx.store.read(venue, symbol, timeframe, venue=venue, last=limit)
        except FileNotFoundError:
            candles = []
        fills: list[dict[str, Any]] = []
        if candles:
            async with ctx.sessions() as session:
                rows = (
                    await session.execute(
                        select(FillRow, OrderRow.reason)
                        .join(OrderRow, OrderRow.client_order_id == FillRow.client_order_id)
                        .where(
                            FillRow.mode == MODE,
                            FillRow.venue == venue,
                            FillRow.symbol == symbol,
                            FillRow.ts >= candles[0].open_time,
                        )
                        .order_by(FillRow.ts)
                    )
                ).all()
            fills = [
                {
                    "ts": f.ts.isoformat(),
                    "side": f.side,
                    "price": float(f.price),
                    "quantity": float(f.quantity),
                    "reason": reason,
                }
                for f, reason in rows
            ]
        pf = await portfolio()
        position = None
        if pf is not None and pf.position(venue, symbol).is_open:
            p = pf.position(venue, symbol)
            position = {
                "quantity": float(p.quantity),
                "avg_price": float(p.avg_price),
                "stop_loss": _f(p.stop_loss),
                "take_profit": _f(p.take_profit),
            }
        return {
            "market": market,
            "timeframe": timeframe,
            "tf_seconds": seconds,
            "bars": [
                {
                    "time": int(b.open_time.timestamp()),
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                }
                for b in candles
            ],
            "fills": fills,
            "position": position,
        }

    @app.get("/api/backtests")
    async def backtests() -> list[dict[str, Any]]:
        root = ctx.settings.data_dir / "reports"
        result = []
        for folder in sorted(root.glob("*_*"), reverse=True) if root.exists() else []:
            summary_path = folder / "summary.json"
            if not summary_path.is_file():
                continue
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                created = datetime.strptime(folder.name.split("_", 1)[0], "%Y%m%d-%H%M%S")
            except (ValueError, json.JSONDecodeError):
                continue
            result.append(
                {
                    "id": folder.name,
                    "strategy": summary.get("strategy"),
                    "created_at": created.replace(tzinfo=UTC).isoformat(),
                    "report_url": f"/reports/{folder.name}/report.html",
                    "markets": [
                        {
                            "market": m["market"],
                            "timeframe": m.get("timeframe"),
                            "from": m.get("from"),
                            "to": m.get("to"),
                            **m["metrics"],
                        }
                        for m in summary.get("markets", [])
                    ],
                }
            )
        return result

    # --- tiempo real ---------------------------------------------------------------------

    @app.websocket("/api/ws")
    async def websocket(ws: WebSocket) -> None:
        origin = ws.headers.get("origin")
        if origin and urlparse(origin).hostname not in ALLOWED_HOSTS:
            await ws.close(code=1008)
            return
        await ws.accept()
        queue = hub.connect()
        try:
            await ws.send_json({"type": "hello", "running": ctx.engine is not None})
            while (message := await queue.get()) is not CLOSE:
                await ws.send_json(message)
            await ws.close()
        except WebSocketDisconnect:
            pass
        finally:
            hub.disconnect(queue)

    # --- ficheros ------------------------------------------------------------------------

    reports = ctx.settings.data_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    app.mount("/reports", StaticFiles(directory=reports), name="reports")
    if (FRONTEND_DIST / "index.html").is_file():
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="panel")
    else:

        @app.get("/", response_class=HTMLResponse)
        async def not_built() -> str:
            return (
                "<h1>Panel sin construir</h1><p>Ejecuta <code>npm --prefix frontend install</code>"
                " y <code>npm --prefix frontend run build</code>, y recarga.</p>"
            )

    return app
