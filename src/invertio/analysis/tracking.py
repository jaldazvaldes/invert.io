"""Seguimiento hipotético de niveles congelados; nunca representa una ejecución."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from invertio.core.models import Bar

HORIZONS = {"15m": 15, "1h": 60, "4h": 240}


def timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def hypothetical_return(opportunity: dict[str, Any], price: float) -> float:
    """La entrada ya incluye el lado ask. Solo proyecta fricción de salida y ambas tasas."""
    costs = opportunity["costs"]
    fee = float(costs.get("fee_pct", opportunity["config"]["taker_fee_pct"])) / 100
    exit_factor = float(costs.get("exit_price_factor", 1))
    spent = opportunity["reference_eur"]
    proceeds = opportunity["quantity"] * price * exit_factor * (1 - fee)
    return float((proceeds - spent * (1 + fee)) / spent * 100)


def finish(
    opportunity: dict[str, Any],
    outcome: str,
    now: datetime,
    reason: str,
    *,
    price: float | None = None,
    quality: str = "complete",
) -> None:
    if opportunity["status"] != "open":
        return
    opportunity.update(
        status="interrupted" if outcome == "interrupted" else "closed",
        outcome=outcome,
        quality=quality,
        ended_at=now.isoformat(),
        updated_at=now.isoformat(),
        reason=reason,
        end_price=price,
        estimated_return_pct=(
            hypothetical_return(opportunity, price)
            if price is not None and quality == "complete"
            else None
        ),
    )


def track_bars(opportunity: dict[str, Any], bars: list[Bar], now: datetime) -> None:
    """No usa la vela de entrada (su máximo/mínimo puede preceder al aviso)."""
    created = timestamp(opportunity["created_at"])
    cursor = timestamp(opportunity.get("last_tracked_at", opportunity["created_at"]))
    deadline = created + timedelta(minutes=opportunity["config"]["lifetime_minutes"])
    complete_bars = [b for b in bars if b.open_time >= created and b.close_time <= now]
    for bar in complete_bars:
        if bar.close_time <= cursor:
            continue
        # El primer minuto parcial se omite; a partir de ahí un hueco no prueba resultados.
        first_open = created.replace(second=0, microsecond=0)
        if first_open < created:
            first_open += timedelta(minutes=1)
        expected_open = first_open if cursor == created else cursor
        if bar.open_time > expected_open:
            opportunity["tracking_gaps"] = True
            finish(
                opportunity,
                "interrupted",
                bar.close_time,
                "Faltan velas durante el seguimiento",
                quality="incomplete",
            )
        cursor = bar.close_time
        opportunity["last_tracked_at"] = cursor.isoformat()
        for label, minutes in HORIZONS.items():
            horizon = opportunity["horizons"][label]
            due = created + timedelta(minutes=minutes)
            if horizon["at"] is None and due <= bar.close_time:
                valid = (
                    bar.volume > 0
                    and (bar.close_time - due).total_seconds() <= 60
                    and not opportunity.get("tracking_gaps", False)
                )
                horizon.update(
                    at=bar.close_time.isoformat(),
                    return_pct=(bar.close / opportunity["entry"] - 1) * 100 if valid else None,
                    net_return_pct=hypothetical_return(opportunity, bar.close) if valid else None,
                    quality="complete" if valid else "incomplete",
                )
        if bar.volume <= 0:
            # El proveedor usa el precio medio bid/ask en minutos sin operaciones.
            # El minuto existe, pero su OHLC no acredita toques ni rentabilidades.
            if bar.close_time >= deadline:
                finish(
                    opportunity,
                    "expired",
                    deadline,
                    "Caducidad sin precio negociado en la última vela",
                    quality="incomplete",
                )
            continue
        # Extremos del periodo observado, incluso después del desenlace, hasta cuatro horas.
        if bar.open_time < created + timedelta(hours=4):
            opportunity["high_water"] = max(opportunity["high_water"], bar.high)
            opportunity["low_water"] = min(opportunity["low_water"], bar.low)
            opportunity["mfe_pct"] = (opportunity["high_water"] / opportunity["entry"] - 1) * 100
            opportunity["mae_pct"] = (opportunity["low_water"] / opportunity["entry"] - 1) * 100
        if opportunity["status"] != "open":
            continue
        # Una vela que cruza la caducidad no permite ordenar un toque posterior a ella.
        if bar.open_time >= deadline:
            finish(
                opportunity,
                "expired",
                deadline,
                "Caducidad del seguimiento",
                price=bar.open if bar.open_time == deadline else None,
                quality="complete" if bar.open_time == deadline else "incomplete",
            )
            continue
        stop_hit, target_hit = bar.low <= opportunity["stop"], bar.high >= opportunity["target"]
        if stop_hit and target_hit:
            finish(
                opportunity,
                "ambiguous",
                bar.close_time,
                "La misma vela toca stop y objetivo; no se conoce el orden",
                quality="ambiguous",
            )
        elif (stop_hit or target_hit) and bar.close_time > deadline:
            finish(
                opportunity,
                "ambiguous",
                deadline,
                "Toque y caducidad dentro de la misma vela",
                quality="ambiguous",
            )
        elif stop_hit:
            finish(
                opportunity,
                "stop",
                bar.close_time,
                "Nivel de stop observado",
                price=min(bar.open, opportunity["stop"]),
            )
        elif target_hit:
            finish(
                opportunity,
                "target",
                bar.close_time,
                "Nivel objetivo observado",
                price=opportunity["target"],
            )
        elif bar.close_time >= deadline:
            finish(
                opportunity,
                "expired",
                deadline,
                "Caducidad del seguimiento",
                price=bar.close if bar.close_time == deadline else None,
                quality="complete" if bar.close_time == deadline else "incomplete",
            )
    for label, minutes in HORIZONS.items():
        horizon = opportunity["horizons"][label]
        if horizon["at"] is None and now > created + timedelta(minutes=minutes + 2):
            # Mantener at=None permite completar más tarde si se recupera historia exacta.
            horizon["quality"] = "incomplete"
    opportunity["updated_at"] = now.isoformat()
