"""Reglas puras para el experimento: sin órdenes, red ni información futura.

Los periodos de un minuto y umbrales son hipótesis experimentales. Las fuentes
explican los indicadores; no validan estos parámetros ni prometen rentabilidad.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from invertio.core.models import Bar
from invertio.core.timeframes import timeframe_delta
from invertio.indicators import ATR, EMA, MACD, RSI
from invertio.simulation.config import SimulationConfig
from invertio.strategies.score import ScoreParams, ScoreStrategy

_WARMUP = {"score_base": 201, "trend_ema": 51, "breakout": 50, "rsi_rebound": 16}
_EMA_SOURCE = {
    "title": "Fidelity: media móvil exponencial (EMA)",
    "url": "https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/ema",
}
_MOMENTUM_SOURCE = {
    "title": "Fidelity: análisis técnico, RSI y MACD",
    "url": "https://www.fidelity.com/learning-center/trading-investing/technical-trading",
}
_VOLUME_SOURCE = {
    "title": "Schwab: volumen y confirmación de tendencias",
    "url": "https://www.schwab.com/learn/story/ways-volume-can-help-confirm-price-trends",
}


def definitions() -> list[dict[str, Any]]:
    """Devuelve reglas versionadas y configuraciones nuevas en cada llamada."""
    common = SimulationConfig().model_dump(mode="json")
    result: list[dict[str, Any]] = [
        {
            "id": "score_base",
            "name": "Puntuación base",
            "description": "La combinación original de tendencia, momentum, MACD, RSI y volumen.",
            "rules": [
                "Entrada con puntuación de reglas ≥ 70; salida con puntuación ≤ 40.",
                "Stop a 2 ATR, objetivo a 3 ATR y caducidad de 240 minutos.",
            ],
            "simulation_config": dict(common),
            "sources": [dict(_EMA_SOURCE), dict(_MOMENTUM_SOURCE), dict(_VOLUME_SOURCE)],
        },
        {
            "id": "trend_ema",
            "name": "Cruce de tendencia",
            "description": "Busca un cruce alcista de medias confirmado por MACD.",
            "rules": [
                "EMA 20 cruza por encima de EMA 50 y el histograma MACD (12,26,9) es positivo.",
                "Salida cuando EMA 20 cae por debajo de EMA 50.",
                "Stop a 2 ATR, objetivo a 4 ATR y caducidad de 240 minutos.",
            ],
            "simulation_config": {**common, "target_atr": "4"},
            "sources": [dict(_EMA_SOURCE), dict(_MOMENTUM_SOURCE)],
        },
        {
            "id": "breakout",
            "name": "Ruptura con volumen",
            "description": "Busca máximos nuevos acompañados de mayor actividad y tendencia.",
            "rules": [
                "Cierre por encima del máximo de las 20 velas anteriores y EMA 20 > EMA 50.",
                "Volumen ≥ 1,5 veces la media de las 20 velas anteriores, sin incluir la actual.",
                "Salida al cerrar por debajo de EMA 20.",
                "Stop a 2 ATR, objetivo a 4 ATR y caducidad de 240 minutos.",
            ],
            "simulation_config": {**common, "target_atr": "4"},
            "sources": [dict(_EMA_SOURCE), dict(_VOLUME_SOURCE)],
        },
        {
            "id": "rsi_rebound",
            "name": "Rebote de RSI",
            "description": "Busca recuperación del impulso después de un RSI bajo.",
            "rules": [
                "RSI 14 cruza por encima de 30 tras estar en 30 o menos y el cierre sube.",
                "Salida al alcanzar RSI 14 ≥ 55.",
                "Stop a 2 ATR, objetivo a 3 ATR y caducidad de 120 minutos.",
            ],
            "simulation_config": {**common, "lifetime_minutes": 120},
            "sources": [dict(_MOMENTUM_SOURCE)],
        },
        {
            "id": "cash",
            "name": "Referencia: conservar euros",
            "description": "Mantener los 50 € ficticios en efectivo, sin compras ni comisiones.",
            "rules": ["No opera: sirve de comparación con todas las estrategias."],
            "simulation_config": dict(common),
            "sources": [],
        },
    ]
    for definition in result:
        definition["version"] = "prospective-v1"
        definition["warmup_bars"] = _WARMUP.get(definition["id"], 0)
        definition["experimental"] = True
        if definition["id"] != "cash":
            definition["rules"].append(
                "Velas cerradas de 1 minuto; sin señal nueva en velas sin volumen. "
                "ATR de 14 periodos."
            )
    return result


def _empty(strategy_id: str, reason: str) -> dict[str, Any]:
    return {
        "ready": False,
        "enter": False,
        "exit": False,
        "entry_reason": reason,
        "exit_reason": reason,
        "indicators": {},
        "bar_ts": None,
        "atr": None,
        "warmup_bars": _WARMUP[strategy_id],
    }


def _closed_history(
    bars: list[Bar], now: datetime, timeframe: str = "1m"
) -> tuple[list[Bar], str | None]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now debe incluir zona horaria")
    closed = sorted((bar for bar in bars if bar.close_time <= now), key=lambda bar: bar.open_time)
    if not closed:
        return [], "Aún no hay velas cerradas"
    last = closed[-1]
    identity = (last.venue, last.symbol, last.timeframe)
    if last.timeframe != timeframe:
        return [], (
            "Las reglas requieren velas de 1 minuto"
            if timeframe == "1m"
            else f"Las reglas requieren velas de {timeframe}"
        )
    if last.venue != "revolutx" or not last.symbol.endswith("/EUR"):
        return [], "Las reglas requieren datos Revolut X en EUR"
    for bar in closed:
        if (bar.venue, bar.symbol, bar.timeframe) != identity:
            return [], "No se pueden mezclar mercados ni orígenes"
        values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
        if (
            not all(math.isfinite(value) for value in values)
            or min(values[:4]) <= 0
            or bar.volume < 0
            or bar.low > min(bar.open, bar.close)
            or bar.high < max(bar.open, bar.close)
        ):
            return [], "La serie contiene una vela inválida"
    step = timeframe_delta(timeframe)
    start = 0
    for index in range(1, len(closed)):
        gap = closed[index].open_time - closed[index - 1].open_time
        if gap == timedelta(0):
            return [], "La serie contiene velas duplicadas"
        if gap != step:
            start = index
    return closed[start:], None


def evaluate(strategy_id: str, bars: list[Bar], now: datetime) -> dict[str, Any]:
    """Evalúa únicamente el prefijo cerrado, con calentamiento y precio observable.

    No genera puntuaciones para las estrategias que no usan ScoreStrategy. Los
    stops, objetivos, ejecución al libro y gestión del capital corresponden al
    simulador. Los ceros de volumen conservan el calendario, pero no autorizan
    entradas ni salidas por indicadores; los cruces requieren dos velas negociadas.
    """
    if strategy_id not in _WARMUP:
        raise ValueError(f"Estrategia desconocida o no operativa: {strategy_id}")
    closed, error = _closed_history(bars, now)
    result = _empty(strategy_id, error or "Preparando indicadores")
    if error:
        return result
    current = closed[-1]
    result["bar_ts"] = current.close_time.isoformat()
    if len(closed) < _WARMUP[strategy_id]:
        result["entry_reason"] = result["exit_reason"] = (
            f"Preparando indicadores: {len(closed)}/{_WARMUP[strategy_id]} velas consecutivas"
        )
        return result
    if now - current.close_time > timedelta(seconds=120):
        result["entry_reason"] = result["exit_reason"] = (
            "La última vela cerrada tiene más de 2 minutos"
        )
        return result
    return signal(strategy_id, closed, result)


def signal(strategy_id: str, closed: list[Bar], result: dict[str, Any]) -> dict[str, Any]:
    """Indicadores y reglas sobre una serie cerrada, consecutiva y ya calentada.

    Es independiente del periodo de las velas: las variantes de 10 minutos y de
    una hora aplican exactamente las mismas condiciones que las de un minuto.
    """
    current = closed[-1]
    atr_indicator = ATR(14)
    fast, slow, macd, rsi = EMA(20), EMA(50), MACD(), RSI(14)
    score_strategy = ScoreStrategy(ScoreParams()) if strategy_id == "score_base" else None
    previous: dict[str, float | None] = {}
    indicators: dict[str, float | None] = {}
    atr: float | None = None
    for bar in closed:
        previous = indicators
        atr = atr_indicator.update(bar.high, bar.low, bar.close)
        indicators = {
            "close": bar.close,
            "volume": bar.volume,
            "ema20": fast.update(bar.close),
            "ema50": slow.update(bar.close),
            "macd_histogram": macd.update(bar.close),
            "rsi14": rsi.update(bar.close),
        }
        if score_strategy is not None:
            score = score_strategy.evaluate(bar)
            indicators["score"] = score.score if score else None
    result.update(ready=True, indicators=indicators, atr=atr)
    if atr is None or not math.isfinite(atr) or atr <= 0:
        result.update(ready=False, entry_reason="ATR no disponible o nulo", exit_reason="")
        return result
    if current.volume <= 0:
        result.update(entry_reason="Última vela sin volumen: sin señal", exit_reason="")
        return result

    enter, leave = False, False
    entry_reason, exit_reason = "No se cumple la condición de entrada", ""
    if strategy_id == "score_base":
        score_value = indicators["score"]
        assert score_value is not None
        enter, leave = score_value >= 70, score_value <= 40
        entry_reason = f"Puntuación {score_value:g}/100; entrada con 70 o más"
        exit_reason = f"Puntuación {score_value:g}/100; salida con 40 o menos" if leave else ""
    elif strategy_id == "trend_ema":
        ema20, ema50 = indicators["ema20"], indicators["ema50"]
        prev_fast, prev_slow = previous["ema20"], previous["ema50"]
        histogram = indicators["macd_histogram"]
        assert all(value is not None for value in (ema20, ema50, prev_fast, prev_slow, histogram))
        assert ema20 is not None and ema50 is not None and histogram is not None
        assert prev_fast is not None and prev_slow is not None
        enter = prev_fast <= prev_slow and ema20 > ema50 and histogram > 0 and closed[-2].volume > 0
        leave = ema20 < ema50
        if enter:
            entry_reason = "EMA 20 cruza EMA 50 al alza con histograma MACD positivo"
        if leave:
            exit_reason = "EMA 20 está por debajo de EMA 50"
        indicators.update(previous_ema20=prev_fast, previous_ema50=prev_slow)
    elif strategy_id == "breakout":
        window = closed[-21:-1]
        previous_high = max(bar.high for bar in window)
        average_volume = sum(bar.volume for bar in window) / len(window)
        ema20, ema50 = indicators["ema20"], indicators["ema50"]
        assert ema20 is not None and ema50 is not None
        enter = (
            current.close > previous_high
            and average_volume > 0
            and current.volume >= 1.5 * average_volume
            and ema20 > ema50
        )
        leave = current.close < ema20
        indicators.update(previous_high20=previous_high, previous_volume20=average_volume)
        if enter:
            entry_reason = (
                "Ruptura del máximo de 20 velas con volumen ≥ 1,5 × media y EMA 20 > EMA 50"
            )
        if leave:
            exit_reason = "El cierre está por debajo de EMA 20"
    else:
        rsi_now, rsi_previous = indicators["rsi14"], previous["rsi14"]
        assert rsi_now is not None and rsi_previous is not None
        enter = (
            rsi_previous <= 30 < rsi_now
            and current.close > closed[-2].close
            and closed[-2].volume > 0
        )
        leave = rsi_now >= 55
        indicators["previous_rsi14"] = rsi_previous
        if enter:
            entry_reason = "RSI 14 recupera 30 desde abajo y el cierre sube"
        if leave:
            exit_reason = f"RSI 14 alcanza {rsi_now:.2f}; salida con 55 o más"
    result.update(enter=enter, exit=leave, entry_reason=entry_reason, exit_reason=exit_reason)
    return result
