"""Estrategias que han aprobado el laboratorio, observadas en vivo con dinero ficticio.

Se usan las mismas clases de estrategia que en el laboratorio (`on_bar`), con los parámetros
elegidos allí y sus mismas velas cerradas de Revolut X. Como en el laboratorio:
- la compra es una orden límite pasiva (maker, 0 %) que caduca a las dos velas;
- no hay objetivo de beneficio ni caducidad: se sale por la señal de la estrategia o por stop;
- el filtro de BTC (strategies/regime.py), si lo hay, usa el último cierre diario de BTC,
  formado aquí uniendo sus seis velas de 4 horas de cada día UTC.

Aprobar el laboratorio no garantiza nada: esta observación en vivo es la prueba siguiente.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from invertio.core.models import Bar, Position, SignalAction
from invertio.core.timeframes import timeframe_delta
from invertio.experiments import policies
from invertio.simulation.config import SimulationConfig
from invertio.strategies import STRATEGIES
from invertio.strategies.base import Strategy
from invertio.strategies.regime import btc_regime
from invertio.strategies.score import points_str

VERSION = "lab-v1"
DAILY_SOURCE = "4h"  # velas con las que se forman los días de BTC para el filtro
_OVERDUE_GRACE = timedelta(minutes=5)
_ENTRY_MAX_DELAY = timedelta(minutes=5)
_TTL_BARS = 2  # `limit_ttl_bars` del laboratorio: la compra pasiva espera dos velas
_NO_TARGET_ATR = "1000"  # sin objetivo: el laboratorio no usa take-profit en estas reglas
_NO_EXPIRY_MINUTES = 525_600  # un año: el laboratorio no tiene caducidad
_LAB_RUN = "data/lab/20260929-083929 (test: 29/09/2025 – 29/09/2026)"


def _lab(median: float, positive: float, trades: float, per_trade: float) -> dict[str, Any]:
    return {
        "run": _LAB_RUN,
        "median_return_pct": median,
        "positive_pct": positive,
        "trades_per_coin": trades,
        "median_trade_pct": per_trade,
        "buy_and_hold_pct": -33.0,
    }


_SCORE = "Suma puntos de tendencia, momento, MACD, RSI, volumen y ruptura (0 a 100)."
_BREAKOUT = "Compra al cerrar por encima del máximo de las últimas N velas (canal de Donchian)."

# Las cinco combinaciones que aprobaron (mejor de su grupo en entrenamiento, juzgada en test).
VARIANTS: dict[str, dict[str, Any]] = {
    "score_4h": {
        "name": "Puntuación 4 h",
        "description": f"{_SCORE} Sin filtro de BTC.",
        "strategy": "puntuacion",
        "timeframe": "4h",
        "params": {
            "entry_score": 70,
            "exit_score": 30,
            "min_atr_pct": 0.5,
            "stop_atr": 1.5,
            "take_profit_atr": None,
        },
        "btc_filter": None,
        "lab_result": _lab(0.70, 60, 25.1, -0.08),
    },
    "score_4h_btc50": {
        "name": "Puntuación 4 h · BTC 50 días",
        "description": f"{_SCORE} Solo compra con BTC sobre su media de 50 días.",
        "strategy": "puntuacion",
        "timeframe": "4h",
        "params": {
            "entry_score": 60,
            "exit_score": 30,
            "min_atr_pct": 0.5,
            "stop_atr": 1.5,
            "take_profit_atr": None,
        },
        "btc_filter": 50,
        "lab_result": _lab(1.99, 62, 24.7, -0.16),
    },
    "score_4h_btc200": {
        "name": "Puntuación 4 h · BTC 200 días",
        "description": f"{_SCORE} Solo compra con BTC sobre su media de 200 días.",
        "strategy": "puntuacion",
        "timeframe": "4h",
        "params": {
            "entry_score": 70,
            "exit_score": 30,
            "min_atr_pct": 0.15,
            "stop_atr": 1.5,
            "take_profit_atr": None,
        },
        "btc_filter": 200,
        "lab_result": _lab(1.68, 73, 7.1, 0.03),
    },
    "breakout_1h_btc50": {
        "name": "Ruptura 1 h · BTC 50 días",
        "description": f"{_BREAKOUT} Solo compra con BTC sobre su media de 50 días.",
        "strategy": "ruptura",
        "timeframe": "1h",
        "params": {"entry_bars": 55, "exit_bars": 50, "stop_atr": 2},
        "btc_filter": 50,
        "lab_result": _lab(2.93, 60, 29.9, 0.27),
    },
    "breakout_1h_btc200": {
        "name": "Ruptura 1 h · BTC 200 días",
        "description": f"{_BREAKOUT} Solo compra con BTC sobre su media de 200 días.",
        "strategy": "ruptura",
        "timeframe": "1h",
        "params": {"entry_bars": 100, "exit_bars": 50, "stop_atr": 4},
        "btc_filter": 200,
        "lab_result": _lab(2.13, 73, 6.7, 2.56),
    },
}


type Variants = dict[str, dict[str, Any]]


def timeframe(strategy_id: str, variants: Variants | None = None) -> str:
    return str((VARIANTS if variants is None else variants)[strategy_id]["timeframe"])


def _strategy(strategy_id: str, variants: Variants | None = None) -> Strategy[Any]:
    variant = (VARIANTS if variants is None else variants)[strategy_id]
    cls = STRATEGIES[variant["strategy"]]
    return cls(cls.params_model.model_validate(variant["params"]))


def _minutes(tf: str) -> int:
    return int(timeframe_delta(tf).total_seconds() // 60)


def _label(tf: str) -> str:
    return "4 horas" if tf == "4h" else "1 hora" if tf == "1h" else tf


def definitions(variants: Variants | None = None, version: str = VERSION) -> list[dict[str, Any]]:
    result = []
    variants = VARIANTS if variants is None else variants
    for ident, variant in variants.items():
        strategy = _strategy(ident, variants)
        maker = variant.get("execution", "maker_entry") == "maker_entry"
        tf, btc_days = variant["timeframe"], variant.get("btc_filter")
        stop_atr = variant["params"]["stop_atr"]
        pending = _TTL_BARS * _minutes(tf)
        if variant["strategy"] == "puntuacion":
            p = variant["params"]
            rule = (
                f"Compra cuando la puntuación cruza {p['entry_score']:g} hacia arriba y el ATR "
                f"es al menos el {points_str(p['min_atr_pct'])} % del precio; vende cuando la "
                f"puntuación baja a {p['exit_score']:g} o menos."
            )
            atr = "ATR de 14 velas"
        elif variant["strategy"] == "ruptura_dinamica":
            p = variant["params"]
            rule = (
                f"Compra al cerrar por encima del máximo de las {p['entry_bars']} velas "
                "anteriores; vende al cerrar por debajo del máximo de las últimas "
                f"{p.get('trail_bars', 22)} velas menos {points_str(p['stop_atr'])} ATR: un "
                "stop que sube con el precio y deja correr las ganancias."
            )
            atr = "ATR de 22 velas"
        else:
            p = variant["params"]
            rule = (
                f"Compra al cerrar por encima del máximo de las {p['entry_bars']} velas "
                f"anteriores; vende al cerrar por debajo del mínimo de las {p['exit_bars']}."
            )
            atr = "ATR de 20 velas"
        entry = (
            "Compra con orden límite pasiva (maker, 0 %) al mejor precio de compra; si en "
            f"{_TTL_BARS} velas ({pending // 60} h) ningún libro posterior la cruza, se "
            "cancela. Venta inmediata (taker, 0,09 %)."
            if maker
            else "Compra y venta inmediatas al libro (taker, 0,09 % por lado), sin esperar a "
            "que el precio baje hasta una orden pasiva."
        )
        rules = [
            rule,
            f"Stop a {points_str(stop_atr)} ATR ({atr}), sin objetivo de beneficio ni caducidad.",
            f"Velas cerradas de {_label(tf)} de Revolut X.",
            entry,
            "Stop y datos se revisan cada minuto con el libro de órdenes.",
        ]
        if btc_days:
            rules.insert(
                1,
                f"Solo compra si el último cierre diario de BTC está por encima de su media "
                f"de {btc_days} días. Las ventas no se bloquean.",
            )
        definition: dict[str, Any] = {
            "id": ident,
            "name": variant["name"],
            "description": variant["description"],
            "rules": rules,
            "simulation_config": SimulationConfig(
                stop_atr=Decimal(str(stop_atr)),
                target_atr=Decimal(_NO_TARGET_ATR),
                lifetime_minutes=_NO_EXPIRY_MINUTES,
            ).model_dump(mode="json"),
            "sources": [],
            "version": version,
            "experimental": True,
            "execution": "maker_entry" if maker else "taker",
        }
        if maker:
            definition["pending_minutes"] = pending
        result.append(
            {
                **definition,
                "timeframe_minutes": _minutes(tf),
                "strategy": variant["strategy"],
                "btc_filter": btc_days,
                "lab_params": copy.deepcopy(variant["params"]),
                "lab_result": copy.deepcopy(variant["lab_result"]),
                "warmup_bars": strategy.warmup_bars,
            }
        )
    return result


def daily_from_4h(bars: list[Bar]) -> list[Bar]:
    """Velas diarias UTC completas uniendo sus seis velas de 4 horas; sin huecos."""
    period = timedelta(hours=4)
    days: dict[datetime, list[Bar]] = {}
    for bar in bars:
        start = bar.open_time.replace(hour=0, minute=0, second=0, microsecond=0)
        days.setdefault(start, []).append(bar)
    result = []
    for start in sorted(days):
        group = sorted(days[start], key=lambda bar: bar.open_time)
        if [bar.open_time for bar in group] != [start + i * period for i in range(6)]:
            continue
        result.append(
            Bar(
                venue=group[0].venue,
                symbol=group[0].symbol,
                timeframe="1d",
                open_time=start,
                open=group[0].open,
                high=max(bar.high for bar in group),
                low=min(bar.low for bar in group),
                close=group[-1].close,
                volume=sum(bar.volume for bar in group),
            )
        )
    return result


class _Context:
    """Posición simulada para preguntar a la estrategia: ¿compraría?, ¿vendería?"""

    def __init__(self, holding: bool) -> None:
        self.holding = holding

    def position(self, venue: str, symbol: str) -> Position:
        position = Position(venue, symbol)
        if self.holding:
            position.quantity = Decimal(1)
        return position


def _empty(warmup: int, reason: str) -> dict[str, Any]:
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
        "warmup_bars": warmup,
    }


def evaluate(
    strategy_id: str,
    native: list[Bar],
    minute: list[Bar],
    btc_daily: list[Bar],
    now: datetime,
    *,
    cache: dict[tuple[str, str], tuple[Any, dict[str, Any]]] | None = None,
    variants: Variants | None = None,
) -> dict[str, Any]:
    """Señal de la última vela cerrada; la frescura la marca la vela de un minuto."""
    variants = VARIANTS if variants is None else variants
    if strategy_id not in variants:
        raise ValueError(f"Estrategia desconocida o no operativa: {strategy_id}")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now debe incluir zona horaria")
    tf = timeframe(strategy_id, variants)
    period = timeframe_delta(tf)
    warmup = _strategy(strategy_id, variants).warmup_bars
    result = _empty(warmup, "Preparando indicadores")
    closed_minutes = [bar for bar in minute if bar.close_time <= now]
    if closed_minutes:
        latest = max(closed_minutes, key=lambda bar: bar.open_time)
        result["current_bar_ts"] = latest.close_time.isoformat()
    series = [bar for bar in native if bar.timeframe == tf and bar.close_time <= now]
    closed, error = policies._closed_history(series, now, tf)
    if error:
        result["entry_reason"] = result["exit_reason"] = error
        return result
    current = closed[-1]
    result["signal_bar_ts"] = result["bar_ts"] = current.close_time.isoformat()
    if len(closed) < warmup:
        result["entry_reason"] = result["exit_reason"] = (
            f"Preparando indicadores: {len(closed)}/{warmup} velas consecutivas de {_label(tf)}"
        )
        return result
    if now - current.close_time > period + _OVERDUE_GRACE:
        result["entry_reason"] = result["exit_reason"] = (
            f"Falta la vela de {_label(tf)} más reciente"
        )
        return result
    # Recalcular cientos de velas cada minuto no cambia nada hasta que cierra otra.
    slot = (strategy_id, current.symbol)
    fingerprint = (current.open_time, len(closed), closed[0].open_time, len(btc_daily))
    cached = cache.get(slot) if cache is not None else None
    if cached is not None and cached[0] == fingerprint:
        signal = cached[1]
    else:
        signal = _signal(strategy_id, closed, btc_daily, variants)
        if cache is not None:
            cache[slot] = (fingerprint, signal)
    result.update(copy.deepcopy(signal))
    if result["enter"] and now - current.close_time > _ENTRY_MAX_DELAY:
        result.update(
            enter=False,
            entry_reason="La vela de la señal cerró hace más de 5 minutos: no se abre posición",
        )
    return result


def _signal(
    strategy_id: str, closed: list[Bar], btc_daily: list[Bar], variants: Variants
) -> dict[str, Any]:
    variant = variants[strategy_id]
    strategy = _strategy(strategy_id, variants)
    flat = _Context(holding=False)
    for bar in closed[:-1]:
        strategy.on_bar(bar, flat)
    # Las estrategias guardan sus indicadores al margen de la posición: se pregunta
    # por la última vela a dos copias, una sin posición (compra) y otra con ella (venta).
    current = closed[-1]
    buying = copy.deepcopy(strategy).on_bar(current, flat)
    selling = strategy.on_bar(current, _Context(holding=True))
    buy = next((s for s in buying if s.action is SignalAction.BUY), None)
    sell = next((s for s in selling if s.action is SignalAction.CLOSE), None)
    stop_atr = float(variant["params"]["stop_atr"])
    indicators: dict[str, Any] = {
        "close": current.close,
        "timeframe_minutes": _minutes(variant["timeframe"]),
    }
    last_result = getattr(strategy, "last_result", None)
    score = last_result(current.venue, current.symbol) if callable(last_result) else None
    if score is not None:
        indicators.update(score=score.score, atr_pct=score.atr_pct)
    atr = None
    entry_reason = "No se cumple la condición de entrada"
    if score is not None:
        entry_reason = (
            f"Puntuación {points_str(score.score)}/100: compra al cruzar "
            f"{variant['params']['entry_score']:g} hacia arriba"
        )
    if buy is not None and buy.stop_loss is not None:
        # El stop de la señal es precio − k·ATR: de ahí sale el mismo ATR para el simulador.
        atr = (buy.price - buy.stop_loss) / stop_atr
        entry_reason = buy.reason[:1].upper() + buy.reason[1:]
    enter = buy is not None and atr is not None and atr > 0
    btc_days = variant.get("btc_filter")
    if btc_days:
        allowed = btc_regime(btc_daily, btc_days)(current.close_time)
        indicators["btc_filter_on"] = allowed
        if enter and not allowed:
            enter = False
            entry_reason = (
                f"Señal de compra descartada: BTC cerró por debajo de su media de {btc_days} "
                "días (o falta historia)"
            )
    return {
        "ready": True,
        "enter": enter,
        "exit": sell is not None,
        "atr": atr,
        "indicators": indicators,
        "entry_reason": entry_reason,
        "exit_reason": (sell.reason[:1].upper() + sell.reason[1:]) if sell is not None else "",
    }
