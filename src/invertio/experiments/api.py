"""Consulta de experimentos ficticios; no contiene rutas que ejecuten operaciones."""

from __future__ import annotations

import copy
import csv
import io
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

from invertio.api.context import ApiContext
from invertio.experiments.service import KEY, ExperimentsService, present
from invertio.simulation.repository import SimulationRepository


def register_experiment_routes(
    app: FastAPI, ctx: ApiContext, *, prefix: str = "/api/experiments",
    service_attr: str = "experiments", repository_key: str = KEY,
    filename: str = "comparacion-estrategias.csv",
    include_maker_orders: bool = False,
) -> None:
    repository = SimulationRepository(ctx.sessions, key=repository_key)

    def service() -> ExperimentsService | None:
        value: ExperimentsService | None = getattr(ctx, service_attr)
        return value

    async def saved_state() -> dict[str, Any] | None:
        current = service()
        if current is not None:
            return copy.deepcopy(current.state)
        return await repository.load()

    @app.get(f"{prefix}/status", name=f"{service_attr}_status")
    async def status() -> dict[str, Any]:
        current = service()
        if current is not None:
            return current.status(bool(ctx.analysis and ctx.analysis.running))
        return present(await repository.load(), False, None, datetime.now(UTC))

    @app.get(f"{prefix}/export.csv", name=f"{service_attr}_export")
    async def export(strategy_id: str | None = None) -> Response:
        state = await saved_state()
        fields = [
            "experiment_id",
            "strategy_id",
            "strategy_name",
            "started_at",
            "initial_eur",
            "id",
            "symbol",
            "status",
            "opened_at",
            "closed_at",
            "quantity",
            "entry",
            "exit_price",
            "stop",
            "target",
            "investment_eur",
            "entry_fee_eur",
            "exit_fee_eur",
            "pnl_eur",
            "reason",
            "had_data_gap",
            "source_rule_version",
            "execution",
            "timeframe_minutes",
            "entry_execution_model",
        ]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        catalog = state["definitions"] if state else []
        if strategy_id is not None and strategy_id not in {d["id"] for d in catalog}:
            raise HTTPException(status_code=404, detail="Estrategia no disponible")
        if state:
            for definition in catalog:
                if strategy_id is not None and definition["id"] != strategy_id:
                    continue
                portfolio = state["portfolios"][definition["id"]]
                for trade in portfolio["trades"] + list(portfolio["positions"].values()):
                    values = {
                        **trade,
                        "experiment_id": state["experiment_id"],
                        "strategy_id": definition["id"],
                        "strategy_name": definition["name"],
                        "started_at": state["started_at"],
                        "initial_eur": portfolio["config"]["initial_eur"],
                        "execution": definition.get("execution", "taker"),
                        "timeframe_minutes": definition.get("timeframe_minutes", 1),
                    }
                    row = {key: values.get(key) for key in fields}
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
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    if not include_maker_orders:
        return

    @app.get(f"{prefix}/maker-orders.csv", name=f"{service_attr}_maker_orders")
    async def maker_orders_export(strategy_id: str | None = None) -> Response:
        state = await saved_state()
        catalog = state["definitions"] if state else []
        if strategy_id is not None and strategy_id not in {d["id"] for d in catalog}:
            raise HTTPException(status_code=404, detail="Estrategia no disponible")
        fields = [
            "experiment_id", "strategy_id", "id", "symbol", "status", "placed_at",
            "expires_at", "finished_at", "limit_price", "quantity", "reserved_eur", "reason",
            "incomplete", "placed_book_ts", "fill_book_ts", "execution_model",
        ]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        if state:
            for definition in catalog:
                if strategy_id is not None and definition["id"] != strategy_id:
                    continue
                portfolio = state["portfolios"][definition["id"]]
                orders = portfolio.get("maker_orders", []) + list(
                    portfolio.get("pending_orders", {}).values()
                )
                for order in orders:
                    values = {**order, "experiment_id": state["experiment_id"],
                              "strategy_id": definition["id"]}
                    writer.writerow({
                        field: "'" + value
                        if isinstance(value := values.get(field), str)
                        and value.startswith(("=", "+", "-", "@"))
                        and re.fullmatch(r"-?\d+(\.\d+)?", value) is None
                        else value
                        for field in fields
                    })
        return Response(
            "\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="ordenes-maker-simuladas.csv"'},
        )
