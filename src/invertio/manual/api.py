"""Acciones manuales protegidas por el token de sesión del panel local."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from invertio.api.context import ApiContext
from invertio.manual.service import ManualError, ManualService


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    symbol: str = Field(max_length=40)
    side: Literal["buy", "sell"]
    budget_eur: str | None = Field(default=None, max_length=30)
    quantity: str | None = Field(default=None, max_length=50)


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    preview_id: str = Field(max_length=36)
    confirm: bool


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    order_id: str = Field(max_length=36)
    confirm: bool


def register_manual_routes(app: FastAPI, ctx: ApiContext) -> None:
    def service() -> ManualService:
        if ctx.manual is None:
            raise HTTPException(503, "Operaciones manuales disponibles en invertio analyze")
        return ctx.manual

    def token(x_invertio_token: Annotated[str | None, Header()] = None) -> None:
        if x_invertio_token != ctx.token:
            raise HTTPException(403, "Token de sesión no válido")

    @app.get("/api/manual/status")
    async def status() -> dict[str, Any]:
        if ctx.manual is None:
            return {
                "available": False,
                "configured": False,
                "enabled": False,
                "budget_eur": "50",
                "committed_eur": "0",
                "remaining_eur": "50",
                "balances": [],
                "positions": [],
                "orders": [],
                "error": "Abre el panel del proceso invertio analyze",
            }
        return await ctx.manual.status()

    @app.get("/api/manual/markets")
    async def markets() -> dict[str, Any]:
        try:
            return await service().markets()
        except ManualError as exc:
            raise HTTPException(400, str(exc)) from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(502, "No se pudieron consultar los mercados de Revolut X") from None

    @app.post("/api/manual/refresh", dependencies=[Depends(token)])
    async def refresh() -> dict[str, Any]:
        return await service().refresh()

    @app.post("/api/manual/preview", dependencies=[Depends(token)])
    async def preview(body: PreviewBody) -> dict[str, Any]:
        try:
            return await service().preview(
                body.symbol, body.side, budget_eur=body.budget_eur, quantity=body.quantity
            )
        except ManualError as exc:
            raise HTTPException(400, str(exc)) from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(502, "No se pudo preparar la orden; no se ha enviado") from None

    @app.post("/api/manual/confirm", dependencies=[Depends(token)])
    async def confirm(body: ConfirmBody) -> dict[str, Any]:
        if not body.confirm:
            raise HTTPException(400, "Falta confirmar la operación con dinero real")
        try:
            return await service().confirm(body.preview_id)
        except ManualError as exc:
            raise HTTPException(400, str(exc)) from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                502,
                "No se pudo confirmar el estado; actualiza para conciliar, sin repetir la orden",
            ) from None

    @app.post("/api/manual/cancel", dependencies=[Depends(token)])
    async def cancel(body: CancelBody) -> dict[str, Any]:
        if not body.confirm:
            raise HTTPException(400, "Falta confirmar la cancelación")
        try:
            return await service().cancel(body.order_id)
        except ManualError as exc:
            raise HTTPException(400, str(exc)) from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(502, "Cancelación sin confirmar; actualiza el estado") from None
