"""Grupo «Aprendizajes»: lo nuevo que aprobó el laboratorio y la prueba de compras maker.

Aprendizajes del 29/09/2026 (856 operaciones ficticias y el laboratorio):
- Velas largas, stops fuera del ruido, dejar correr las ganancias y filtro de tendencia.
  De las tres estrategias nuevas solo aprobó la ruptura dinámica de 4 horas con el filtro
  de BTC de 50 días (la compresión y los retrocesos suspendieron).
- En velas de 1 y 5 minutos las compras maker (0 %) salieron peores que las inmediatas:
  se ejecutan justo cuando el precio cae. Aquí se comprueba con velas largas: cada
  estrategia tiene dos cuentas que empiezan a la vez, una maker y otra inmediata.
"""

from __future__ import annotations

import copy
from typing import Any

from invertio.experiments import lab_policies

VERSION = "learned-v1"
_RUN = "data/lab/20260929-142313 (test: 29/09/2025 – 29/09/2026)"
_BTC50 = "Solo compra con BTC sobre su media de 50 días."


def _pair(ident: str, base: dict[str, Any]) -> dict[str, dict[str, Any]]:
    maker = {**copy.deepcopy(base), "name": f"{base['name']} · maker"}
    taker = {**copy.deepcopy(base), "name": f"{base['name']} · inmediata", "execution": "taker"}
    return {f"{ident}_maker": maker, f"{ident}_taker": taker}


VARIANTS: dict[str, dict[str, Any]] = {
    **_pair(
        "trailing_4h_btc50",
        {
            "name": "Ruptura dinámica 4 h · BTC 50 días",
            "description": (
                "Compra al superar el máximo de 55 velas y sale con un stop que sube con el "
                f"precio, sin objetivo: deja correr las ganancias. {_BTC50}"
            ),
            "strategy": "ruptura_dinamica",
            "timeframe": "4h",
            "params": {"entry_bars": 55, "stop_atr": 4},
            "btc_filter": 50,
            "lab_result": {
                "run": _RUN,
                "median_return_pct": 0.40,
                "positive_pct": 69,
                "trades_per_coin": 9.2,
                "median_trade_pct": 0.26,
                "buy_and_hold_pct": -33.0,
            },
        },
    ),
    **_pair("breakout_1h_btc50", lab_policies.VARIANTS["breakout_1h_btc50"]),
    **_pair("score_4h_btc50", lab_policies.VARIANTS["score_4h_btc50"]),
}
