"""Señales comunes para comparar frecuencia y ejecución con capital ficticio.

Maker y taker usan exactamente la misma condición para un periodo dado. Las
reglas sólo leen velas cerradas; la ejecución de órdenes pendientes y el tiempo
de espera después de vender corresponden al servicio de este experimento.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from invertio.core.models import Bar
from invertio.indicators import ATR, RSI
from invertio.simulation.config import SimulationConfig

_STRATEGIES = {
    "rsi_1m_taker": (1, "taker"),
    "rsi_1m_maker": (1, "maker_entry"),
    "rsi_5m_taker": (5, "taker"),
    "rsi_5m_maker": (5, "maker_entry"),
}
_WARMUP = 16
_RSI_SOURCE = {
    "title": "Fidelity: análisis técnico, RSI y MACD",
    "url": "https://www.fidelity.com/learning-center/trading-investing/technical-trading",
}


def definitions() -> list[dict[str, Any]]:
    """Cuatro cuentas nuevas: 1/5 minutos × entrada inmediata/pendiente."""
    common = SimulationConfig().model_dump(mode="json")
    result = []
    for ident, (minutes, execution) in _STRATEGIES.items():
        maker = execution == "maker_entry"
        lifetime = 120 if minutes == 1 else 240
        pending = 5 if minutes == 1 else 15
        cooldown = 0 if minutes == 1 else 15
        rules = [
            f"Velas cerradas de {minutes} minuto(s); RSI 14 y ATR 14.",
            "Compra cuando RSI 14 cruza por encima de 30 tras estar en 30 o menos "
            "y el cierre sube; venta por señal con RSI 14 ≥ 55.",
            f"Stop a 2 ATR, objetivo a 3 ATR y caducidad de {lifetime} minutos.",
            "Sin señales en velas sin volumen; los cruces requieren dos velas negociadas.",
            "50 € ficticios por cuenta; hasta 5 posiciones de 10 € incluyendo comisión.",
        ]
        if maker:
            rules.extend(
                [
                    "Entrada maker hipotética: límite de compra en el mejor bid observado; "
                    "comisión de entrada 0 % y salida inmediata taker al 0,09 %.",
                    f"La compra puede quedar sin ejecutar y caduca a los {pending} minutos. "
                    "No se ejecuta usando el libro que creó la orden.",
                    "Sólo se considera una ejecución hipotética si un libro posterior ofrece "
                    "toda la cantidad estrictamente por debajo del límite. Este modelo no "
                    "conoce la cola de órdenes ni garantiza una ejecución real.",
                ]
            )
        else:
            rules.append(
                "Compra y venta inmediatas simuladas al libro, con comisión taker "
                "del 0,09 % por lado; spread y deslizamiento incluidos en el precio."
            )
        if cooldown:
            rules.append(
                f"Después de vender, espera {cooldown} minutos antes de otra compra "
                "del mismo mercado; necesita una señal nueva."
            )
        else:
            rules.append("Cada compra requiere un cruce nuevo; no repite una señal anterior.")
        result.append(
            {
                "id": ident,
                "name": f"RSI {minutes} min · {'maker' if maker else 'taker'}",
                "description": (
                    f"Rebote de RSI en {minutes} minuto(s), con entrada "
                    f"{'pendiente maker' if maker else 'inmediata taker'}. "
                    "Parámetros experimentales, sin rentabilidad demostrada."
                ),
                "version": "execution-v1",
                "experimental": True,
                "execution": execution,
                "timeframe_minutes": minutes,
                "cooldown_minutes": cooldown,
                "pending_minutes": pending,
                "warmup_bars": _WARMUP,
                "warmup_1m_bars": _WARMUP * minutes,
                "simulation_config": {**common, "lifetime_minutes": lifetime},
                "rules": rules,
                "sources": [dict(_RSI_SOURCE)],
            }
        )
    return result


def _empty(reason: str) -> dict[str, Any]:
    return {
        "ready": False,
        "enter": False,
        "exit": False,
        "entry_reason": reason,
        "exit_reason": reason,
        "indicators": {},
        "signal_bar_ts": None,
        "current_bar_ts": None,
        "bar_ts": None,
        "atr": None,
        "warmup_bars": _WARMUP,
    }


def _history(bars: list[Bar], now: datetime) -> tuple[list[Bar], str | None]:
    """Valida el prefijo cerrado y conserva sólo el último tramo consecutivo."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now debe incluir zona horaria")
    try:
        closed = sorted((bar for bar in bars if bar.close_time <= now), key=lambda b: b.open_time)
    except ValueError:
        return [], "La serie contiene un periodo de vela inválido"
    if not closed:
        return [], "Aún no hay velas cerradas"
    identity = (closed[-1].venue, closed[-1].symbol, closed[-1].timeframe)
    if identity[0] != "revolutx" or not identity[1].endswith("/EUR") or identity[2] != "1m":
        return [], "Las reglas requieren velas de 1 minuto de Revolut X en EUR"
    for bar in closed:
        if (bar.venue, bar.symbol, bar.timeframe) != identity:
            return [], "No se pueden mezclar mercados ni orígenes"
        if bar.open_time.second or bar.open_time.microsecond:
            return [], "Las velas deben estar alineadas a minutos UTC"
        values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
        if (
            not all(math.isfinite(value) for value in values)
            or min(values[:4]) <= 0
            or bar.volume < 0
            or bar.low > min(bar.open, bar.close)
            or bar.high < max(bar.open, bar.close)
        ):
            return [], "La serie contiene una vela inválida"
    start = 0
    for index in range(1, len(closed)):
        gap = closed[index].open_time - closed[index - 1].open_time
        if gap == timedelta(0):
            return [], "La serie contiene velas duplicadas"
        if gap != timedelta(minutes=1):
            start = index
    return closed[start:], None


def _aggregate(closed: list[Bar], minutes: int) -> list[Bar]:
    """Bloques UTC completos; no rellena huecos ni usa bloques parciales."""
    if minutes == 1:
        return closed
    grouped: dict[datetime, list[Bar]] = {}
    for bar in closed:
        start = bar.open_time.replace(minute=(bar.open_time.minute // minutes) * minutes)
        grouped.setdefault(start, []).append(bar)
    result = []
    for start, group in grouped.items():
        if len(group) != minutes or any(
            bar.open_time != start + timedelta(minutes=index) for index, bar in enumerate(group)
        ):
            continue
        result.append(
            Bar(
                venue=group[0].venue,
                symbol=group[0].symbol,
                timeframe=f"{minutes}m",
                open_time=start,
                open=group[0].open,
                high=max(bar.high for bar in group),
                low=min(bar.low for bar in group),
                close=group[-1].close,
                volume=sum(bar.volume for bar in group),
            )
        )
    return result


def evaluate(strategy_id: str, bars: list[Bar], now: datetime) -> dict[str, Any]:
    """Calcula la misma señal por periodo, sin escoger el modelo de ejecución.

    ``signal_bar_ts`` identifica el cierre utilizado por RSI y ATR; las variantes
    de cinco minutos pueden conservarlo mientras termina el bloque siguiente.
    ``current_bar_ts`` permite validar aparte la frescura del dato de un minuto.
    """
    if strategy_id not in _STRATEGIES:
        raise ValueError(f"Estrategia desconocida o no operativa: {strategy_id}")
    minutes = _STRATEGIES[strategy_id][0]
    closed, error = _history(bars, now)
    result = _empty(error or "Preparando indicadores")
    if error:
        return result
    result["current_bar_ts"] = closed[-1].close_time.isoformat()
    aggregate = _aggregate(closed, minutes)
    if any(not math.isfinite(bar.volume) for bar in aggregate):
        result["entry_reason"] = result["exit_reason"] = "El volumen agregado no es válido"
        return result
    if aggregate:
        result["signal_bar_ts"] = aggregate[-1].close_time.isoformat()
        result["bar_ts"] = result["signal_bar_ts"]
    if now - closed[-1].close_time > timedelta(seconds=120):
        result["entry_reason"] = result["exit_reason"] = (
            "La última vela de un minuto tiene más de 2 minutos"
        )
        return result
    if len(aggregate) < _WARMUP:
        result["entry_reason"] = result["exit_reason"] = (
            f"Preparando indicadores: {len(aggregate)}/{_WARMUP} "
            f"velas completas consecutivas de {minutes} minuto(s)"
        )
        return result
    current = aggregate[-1]
    if now - current.close_time > timedelta(seconds=minutes * 60 + 120):
        result["entry_reason"] = result["exit_reason"] = "La vela de señal está retrasada"
        return result

    rsi, atr_indicator = RSI(14), ATR(14)
    rsi_previous: float | None = None
    rsi_now: float | None = None
    atr: float | None = None
    for bar in aggregate:
        rsi_previous, rsi_now = rsi_now, rsi.update(bar.close)
        atr = atr_indicator.update(bar.high, bar.low, bar.close)
    assert rsi_now is not None and rsi_previous is not None
    if not all(math.isfinite(value) for value in (rsi_now, rsi_previous)) or (
        atr is not None and not math.isfinite(atr)
    ):
        result["entry_reason"] = result["exit_reason"] = "Los indicadores no son finitos"
        return result
    result.update(
        ready=True,
        atr=atr,
        indicators={
            "close": current.close,
            "volume": current.volume,
            "rsi14": rsi_now,
            "previous_rsi14": rsi_previous,
            "timeframe_minutes": minutes,
        },
        entry_reason="No se cumple la condición de entrada",
        exit_reason="",
    )
    if atr is None or not math.isfinite(atr) or atr <= 0:
        result.update(ready=False, entry_reason="ATR no disponible o nulo")
        return result
    if current.volume <= 0:
        result.update(entry_reason="Última vela sin volumen: sin señal")
        return result
    enter = (
        rsi_previous <= 30 < rsi_now
        and current.close > aggregate[-2].close
        and aggregate[-2].volume > 0
    )
    leave = rsi_now >= 55
    result.update(enter=enter, exit=leave)
    if enter:
        result["entry_reason"] = "RSI 14 recupera 30 desde abajo y el cierre sube"
    if leave:
        result["exit_reason"] = f"RSI 14 alcanza {rsi_now:.2f}; salida con 55 o más"
    return result
