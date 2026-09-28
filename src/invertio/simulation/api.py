"""Lectura y exportación de la cartera ficticia. Ninguna acción de compra o venta."""

from __future__ import annotations

import csv
import io
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.responses import Response

from invertio.api.context import ApiContext
from invertio.simulation.repository import SimulationRepository


def register_simulation_routes(app: FastAPI, ctx: ApiContext) -> None:
    repository = SimulationRepository(ctx.sessions)

    async def snapshot() -> dict[str, Any]:
        if ctx.simulation is not None:
            return ctx.simulation.status(bool(ctx.analysis and ctx.analysis.running))
        saved = await repository.load()
        stale = bool(
            saved
            and saved.get("last_cycle_at")
            and (datetime.now(UTC) - datetime.fromisoformat(saved["last_cycle_at"])).total_seconds()
            > 120
        )
        if stale and saved and saved.get("positions"):
            saved["stats"].update(equity_eur=None, unrealized_pnl_eur=None, return_pct=None)
        return {
            **(saved or {}),
            "available": saved is not None,
            "running": False,
            "state": "stopped",
            "last_error": None,
            "valuation_stale": stale,
        }

    @app.get("/api/simulation/status")
    async def status() -> dict[str, Any]:
        result = await snapshot()
        for key, limit in (("trades", 200), ("decisions", 100), ("equity", 2000)):
            result[key] = result.get(key, [])[-limit:]
        result.pop("seen_opportunity_ids", None)
        return result

    @app.get("/api/simulation/export.csv")
    async def export() -> Response:
        state = await snapshot()
        fields = [
            "id",
            "symbol",
            "status",
            "opened_at",
            "closed_at",
            "entry",
            "quantity",
            "stop",
            "target",
            "entry_fee_eur",
            "exit_fee_eur",
            "exit_price",
            "pnl_eur",
            "reason",
            "had_data_gap",
            "source_rule_version",
            "source_created_at",
        ]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        trades = state.get("trades", []) + list(state.get("positions", {}).values())
        for trade in trades:
            row = {key: trade.get(key) for key in fields}
            writer.writerow(
                {
                    key: "'" + value
                    if isinstance(value, str)
                    and value.startswith(("=", "+", "-", "@"))
                    and re.fullmatch(r"-?\d+(\.\d+)?", value) is None
                    else value
                    for key, value in row.items()
                }
            )
        return Response(
            "\ufeff" + output.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="simulacion-ficticia.csv"'},
        )
