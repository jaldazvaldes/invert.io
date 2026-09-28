"""Ensayo independiente de frecuencia y compra pasiva; nunca envía órdenes reales."""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from invertio.core.models import Bar
from invertio.experiments.execution_policies import definitions, evaluate
from invertio.experiments.maker import MakerSimulationEngine
from invertio.experiments.service import ExperimentsService
from invertio.simulation.config import SimulationConfig
from invertio.simulation.engine import SimulationEngine

KEY = "execution-comparison-v1"


class ExecutionExperimentService(ExperimentsService):
    name = "Comisiones y frecuencia"
    identity_prefix = "execution-comparison"
    signal_version = "execution-v1"

    def _catalog(self) -> list[dict[str, Any]]:
        return definitions()

    def _engine(
        self, definition: dict[str, Any], state: dict[str, Any] | None, now: datetime
    ) -> SimulationEngine:
        if definition["execution"] == "maker_entry":
            return MakerSimulationEngine(
                SimulationConfig.model_validate(definition["simulation_config"]),
                state,
                now,
                pending_minutes=definition["pending_minutes"],
            )
        return super()._engine(definition, state, now)

    def _policy(self, ident: str, bars: list[Bar], now: datetime) -> dict[str, Any]:
        return evaluate(ident, bars, now)

    def _stable_policy(
        self, policy: dict[str, Any], cursor: dict[str, Any]
    ) -> dict[str, Any]:
        # El recorte de historia 1m no puede alterar una señal 5m ya cerrada.
        # La calidad y frescura actuales siguen mandando durante los cinco minutos.
        if not policy["ready"] or policy.get("signal_bar_ts") is None:
            return policy
        saved = cursor.get("signal_snapshot")
        if saved is not None and saved["signal_bar_ts"] == policy["signal_bar_ts"]:
            stable: dict[str, Any] = copy.deepcopy(saved)
            stable["current_bar_ts"] = policy["current_bar_ts"]
            return stable
        cursor["signal_snapshot"] = copy.deepcopy(policy)
        return policy
