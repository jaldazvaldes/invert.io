"""Las cuatro estrategias del grupo de un minuto, con velas de 10 minutos y de una hora.

Las condiciones de entrada y salida son exactamente las mismas
(``policies.signal``). Cambia el periodo de las velas y, con él, el ATR que fija
stop y objetivo. La caducidad se cuenta en velas: 240 velas de 10 minutos son
40 horas. Son hipótesis experimentales sin rentabilidad demostrada.

Revolut X no ofrece velas de 10 minutos, así que se unen dos velas nativas de
5 minutos en bloques UTC completos. Las velas de una hora son nativas.
"""

from __future__ import annotations

import copy
import math
from datetime import datetime, timedelta
from typing import Any

from invertio.core.models import Bar
from invertio.experiments import policies

# Periodo de señal en minutos -> (sufijo del id, vela nativa de Revolut X, texto)
PERIODS: dict[int, tuple[str, str, str]] = {
    10: ("10m", "5m", "10 minutos"),
    60: ("1h", "1h", "1 hora"),
}
NATIVE_TIMEFRAMES = {native: minutes for minutes, (_, native, _) in PERIODS.items()}
VERSION = "timeframes-v1"
# Tras el cierre esperado de la siguiente vela, margen para que Revolut X la publique.
_OVERDUE_GRACE = timedelta(minutes=5)
# Una señal vista más tarde no abre posición: el precio ya no es el de la señal.
_ENTRY_MAX_DELAY = timedelta(minutes=5)


def _base() -> dict[str, dict[str, Any]]:
    return {d["id"]: d for d in policies.definitions() if d["id"] != "cash"}


def _strategies() -> dict[str, tuple[str, int]]:
    return {
        f"{base}_{suffix}": (base, minutes)
        for minutes, (suffix, _, _) in PERIODS.items()
        for base in _base()
    }


_STRATEGIES = _strategies()
_WARMUP = {ident: d["warmup_bars"] for ident, d in _base().items()}


def _duration(minutes: int) -> str:
    if minutes % 1440 == 0:
        days = minutes // 1440
        return f"{days} día" if days == 1 else f"{days} días"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hora" if hours == 1 else f"{hours} horas"
    return f"{minutes} minutos"


def native_timeframe(strategy_id: str) -> str:
    return PERIODS[_STRATEGIES[strategy_id][1]][1]


def definitions() -> list[dict[str, Any]]:
    """Ocho cuentas: cuatro estrategias × velas de 10 minutos y de una hora."""
    result = []
    for minutes, (suffix, native, label) in PERIODS.items():
        for base_id, base in _base().items():
            candles = base["simulation_config"]["lifetime_minutes"]
            lifetime = candles * minutes
            old = f"caducidad de {candles} minutos"
            new = f"caducidad de {_duration(lifetime)} ({candles} velas)"
            # La última regla describe las velas de un minuto; se sustituye abajo.
            rules = [rule.replace(old, new) for rule in base["rules"][:-1]]
            if not any(new in rule for rule in rules):
                raise ValueError(f"No se encontró la caducidad de {base_id}")
            origin = (
                "dos velas de 5 minutos de Revolut X unidas en bloques UTC completos"
                if native != "1h"
                else "velas nativas de Revolut X"
            )
            rules += [
                f"Velas cerradas de {label} ({origin}); ATR de 14 velas del mismo periodo. "
                "Sin señal nueva en velas sin volumen.",
                "Entradas y salidas por señal se deciden al cerrar cada vela. Stop, objetivo, "
                "caducidad y calidad de los datos se revisan cada minuto con el libro de "
                "órdenes observado.",
                "Una señal vista más de 5 minutos después del cierre de su vela no abre posición.",
            ]
            result.append(
                {
                    "id": f"{base_id}_{suffix}",
                    "name": f"{base['name']} · {label}",
                    "description": f"{base['description']} Velas de {label}.",
                    "rules": rules,
                    "simulation_config": {
                        **copy.deepcopy(base["simulation_config"]),
                        "lifetime_minutes": lifetime,
                    },
                    "sources": copy.deepcopy(base["sources"]),
                    "version": VERSION,
                    "experimental": True,
                    "base_strategy": base_id,
                    "timeframe_minutes": minutes,
                    "native_timeframe": native,
                    "warmup_bars": base["warmup_bars"],
                }
            )
    return result


def aggregate(native: list[Bar], minutes: int) -> list[Bar]:
    """Une velas nativas en bloques UTC completos; no rellena huecos ni usa bloques parciales."""
    if not native:
        return []
    size = timedelta(minutes=minutes)
    step = native[0].close_time - native[0].open_time
    count = size // step
    if count == 1:
        return native
    grouped: dict[datetime, list[Bar]] = {}
    for bar in sorted(native, key=lambda b: b.open_time):
        epoch = int(bar.open_time.timestamp())
        start = datetime.fromtimestamp(epoch - epoch % (minutes * 60), bar.open_time.tzinfo)
        grouped.setdefault(start, []).append(bar)
    result = []
    for start, group in grouped.items():
        if len(group) != count or any(
            bar.open_time != start + index * step or bar.close_time - bar.open_time != step
            for index, bar in enumerate(group)
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
    strategy_id: str, native: list[Bar], minute: list[Bar], now: datetime
) -> dict[str, Any]:
    """Evalúa la señal con velas largas cerradas; la frescura la marca la vela de 1 minuto.

    ``signal_bar_ts`` es el cierre de la vela larga usada por los indicadores.
    ``current_bar_ts`` es el cierre de la última vela de un minuto: sin datos
    recientes no se entra y una posición abierta pasa a esperar datos.
    """
    if strategy_id not in _STRATEGIES:
        raise ValueError(f"Estrategia desconocida o no operativa: {strategy_id}")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now debe incluir zona horaria")
    base_id, minutes = _STRATEGIES[strategy_id]
    _, native_tf, label = PERIODS[minutes]
    warmup = _WARMUP[base_id]
    result = _empty(warmup, "Preparando indicadores")
    closed_minutes = [bar for bar in minute if bar.close_time <= now]
    if closed_minutes:
        latest = max(closed_minutes, key=lambda bar: bar.open_time)
        result["current_bar_ts"] = latest.close_time.isoformat()
    native_closed = [bar for bar in native if bar.close_time <= now and bar.timeframe == native_tf]
    series = aggregate(native_closed, minutes)
    if any(not math.isfinite(bar.volume) for bar in series):
        result["entry_reason"] = result["exit_reason"] = "El volumen agregado no es válido"
        return result
    timeframe = "1h" if minutes == 60 else f"{minutes}m"
    closed, error = policies._closed_history(series, now, timeframe)
    if error:
        result["entry_reason"] = result["exit_reason"] = error
        return result
    current = closed[-1]
    result["signal_bar_ts"] = result["bar_ts"] = current.close_time.isoformat()
    if len(closed) < warmup:
        result["entry_reason"] = result["exit_reason"] = (
            f"Preparando indicadores: {len(closed)}/{warmup} velas consecutivas de {label}"
        )
        return result
    if now - current.close_time > timedelta(minutes=minutes) + _OVERDUE_GRACE:
        result["entry_reason"] = result["exit_reason"] = f"Falta la vela de {label} más reciente"
        return result
    result = policies.signal(base_id, closed, result)
    result["indicators"]["timeframe_minutes"] = minutes
    if result["enter"] and now - current.close_time > _ENTRY_MAX_DELAY:
        result.update(
            enter=False,
            entry_reason="La vela de la señal cerró hace más de 5 minutos: no se abre posición",
        )
    return result
