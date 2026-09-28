"""Lectura del análisis y exportación; independiente de la cartera paper."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response

from invertio.analysis.repository import AnalysisRepository
from invertio.api.context import ApiContext


def _window(since: datetime | None, until: datetime | None) -> None:
    if any(value is not None and value.tzinfo is None for value in (since, until)):
        raise HTTPException(422, "Las fechas deben incluir zona horaria")
    if since is not None and until is not None and since >= until:
        raise HTTPException(422, "El inicio debe ser anterior al final")


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes: dict[str, int] = {}
    returns = []
    versions: dict[str, int] = {}
    for row in rows:
        outcome = row.get("outcome")
        if outcome:
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        version = row["rule_version"]
        versions[version] = versions.get(version, 0) + 1
        if row["quality"] == "complete" and row.get("estimated_return_pct") is not None:
            returns.append(row["estimated_return_pct"])
    return {
        "total": len(rows),
        "open": sum(r["status"] == "open" for r in rows),
        "closed": sum(r["status"] == "closed" for r in rows),
        "interrupted": sum(r["status"] == "interrupted" for r in rows),
        "complete": sum(r["quality"] == "complete" and r["status"] != "open" for r in rows),
        "incomplete": sum(r["quality"] == "incomplete" for r in rows),
        "ambiguous": sum(r["quality"] == "ambiguous" for r in rows),
        "positive": sum(value > 0 for value in returns),
        "known_results": len(returns),
        "mean_return_pct": sum(returns) / len(returns) if returns else None,
        "outcomes": outcomes,
        "versions": versions,
    }


def register_analysis_routes(app: FastAPI, ctx: ApiContext) -> None:
    repository = AnalysisRepository(ctx.sessions)

    @app.get("/api/analysis/status")
    async def analysis_status() -> dict[str, Any]:
        if ctx.analysis is not None:
            return await ctx.analysis.status()
        saved = (await repository.load_state()).get("service", {})
        counts = await repository.notification_counts()
        return {
            "available": bool(saved),
            "running": False,
            "state": "stopped",
            "timeframe": "1m",
            "interval_seconds": saved.get("config", {}).get("interval_seconds", 60),
            "selected": saved.get("selected", []),
            "coverage": saved.get("coverage"),
            "warmup": {"ready": 0, "total": 0},
            "cycles": saved.get("cycles", 0),
            "last_cycle_at": saved.get("last_cycle_at"),
            "last_error": None,
            "telegram": {"configured": ctx.settings.secrets_status()["telegram"], **counts},
            "config": saved.get("config", {}),
        }

    @app.get("/api/analysis/ranking")
    async def analysis_ranking() -> dict[str, Any]:
        if ctx.analysis is not None:
            return {
                "rows": ctx.analysis.rows,
                "excluded": ctx.analysis.excluded,
                "updated_at": ctx.analysis.last_cycle_at,
            }
        saved = (await repository.load_state()).get("service", {})
        return {
            "rows": saved.get("rows", []),
            "excluded": saved.get("excluded", []),
            "updated_at": saved.get("last_cycle_at"),
        }

    @app.get("/api/analysis/opportunities")
    async def analysis_opportunities(
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = Query(500, ge=1, le=5000),
    ) -> list[dict[str, Any]]:
        _window(since, until)
        return (await repository.opportunities(since=since, until=until))[:limit]

    @app.get("/api/analysis/summary")
    async def analysis_summary(
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, Any]:
        _window(since, until)
        rows = await repository.opportunities(since=since, until=until)
        summary = summarize(rows)
        # Resultados por versión: el agregado nunca oculta cambios de reglas/configuración.
        summary["by_version"] = {
            version: summarize([r for r in rows if r["rule_version"] == version])
            for version in summary["versions"]
        }
        return summary

    @app.get("/api/analysis/export.csv")
    async def analysis_export(
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> Response:
        _window(since, until)
        rows = await repository.opportunities(since=since, until=until)
        fields = [
            "id",
            "market",
            "created_at",
            "ended_at",
            "status",
            "outcome",
            "quality",
            "entry",
            "stop",
            "target",
            "reference_eur",
            "score",
            "estimated_return_pct",
            "mfe_pct",
            "mae_pct",
            "rule_version",
            "reason",
        ]
        fields += [
            f"{name}_{field}"
            for name in ("15m", "1h", "4h")
            for field in ("quality", "net_return_pct")
        ]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            values = {field: row.get(field) for field in fields}
            for name, horizon in row.get("horizons", {}).items():
                for field in ("quality", "net_return_pct"):
                    values[f"{name}_{field}"] = horizon.get(field)
            # CSV seguro incluso si el nombre de un mercado o motivo externo empieza con fórmula.
            values = {
                k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v
                for k, v in values.items()
            }
            writer.writerow(values)
        return Response(
            "\ufeff" + output.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="oportunidades-hipoteticas.csv"'},
        )

    @app.get("/api/analysis/opportunities/{opportunity_id}")
    async def analysis_detail(opportunity_id: str) -> dict[str, Any]:
        opportunity = await repository.opportunity(opportunity_id)
        if opportunity is None:
            raise HTTPException(404, "Oportunidad no encontrada")
        created = datetime.fromisoformat(opportunity["created_at"])
        end = (
            datetime.fromisoformat(opportunity["ended_at"])
            if opportunity.get("ended_at")
            else datetime.now(UTC)
        )
        observations = await repository.observations(
            market=opportunity["market"], limit=1000, since=created, until=end
        )
        return {
            **opportunity,
            "observations": [
                r
                for r in observations
                if created <= datetime.fromisoformat(r["observed_at"]) <= end
            ],
            "notifications": await repository.notifications(opportunity_id),
        }
