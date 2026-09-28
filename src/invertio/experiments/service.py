"""Carteras prospectivas independientes con un único conjunto de libros observados."""

from __future__ import annotations

import copy
import hashlib
import math
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from invertio.analysis.market import AnalysisFeed
from invertio.analysis.tracking import timestamp
from invertio.core.clock import Clock, LiveClock
from invertio.core.models import Bar
from invertio.experiments.policies import definitions, evaluate
from invertio.live.feeds import STABLECOINS
from invertio.simulation.config import SimulationConfig
from invertio.simulation.engine import SimulationEngine
from invertio.simulation.repository import SimulationRepository

KEY = "comparison-v1"


def _fresh(value: Any, now: datetime, seconds: int) -> bool:
    try:
        return 0 <= (now - timestamp(value)).total_seconds() <= seconds
    except (TypeError, ValueError, AttributeError):
        return False


def _filters(row: dict[str, Any], market: dict[str, Any], now: datetime) -> list[str]:
    reasons = []
    if market.get("base") in STABLECOINS:
        reasons.append("Stablecoin excluida de las entradas")
    if market.get("active") is not True or not market.get("spot") or market.get("quote") != "EUR":
        reasons.append("Mercado no activo o fuera de contado EUR")
    if not _fresh(row.get("quote_ts"), now, 60):
        reasons.append("Cotización ausente o antigua")
    for field, label in (("quote_volume", "Volumen en EUR"), ("spread_pct", "Spread")):
        value = row.get(field)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            reasons.append(f"{label} desconocido")
        elif field == "quote_volume" and value <= 0:
            reasons.append("Volumen en EUR nulo")
        elif field == "spread_pct" and value > 0.3:
            reasons.append("Spread superior al límite")
    return reasons


def present(
    saved: dict[str, Any] | None, running: bool, error: str | None, now: datetime
) -> dict[str, Any]:
    """Métricas comparables; una valoración desconocida no recibe puesto ni rentabilidad."""
    if saved is None:
        return {
            "available": False,
            "running": False,
            "state": "stopped",
            "strategies": [],
            "last_error": error,
        }
    result = {
        k: copy.deepcopy(saved.get(k))
        for k in (
            "version",
            "experiment_id",
            "started_at",
            "last_cycle_at",
            "market_count",
            "cycles",
            "universe_history",
            "name",
        )
    }
    stale = bool(saved.get("last_cycle_at") and not _fresh(saved["last_cycle_at"], now, 120))
    result.update(available=True, running=running, last_error=error, valuation_stale=stale)
    result["state"] = (
        "error"
        if error
        else "stopped"
        if not running
        else "waiting_data"
        if stale
        else "running"
        if saved.get("last_cycle_at")
        else "starting"
    )
    result["reference"] = {
        "name": "Mantener efectivo",
        "initial_eur": "50",
        "equity_eur": "50",
        "return_pct": "0",
    }
    strategies = []
    for definition in saved["definitions"]:
        portfolio = copy.deepcopy(saved["portfolios"][definition["id"]])
        stats = portfolio["stats"]
        if stale and portfolio["positions"]:
            stats.update(equity_eur=None, unrealized_pnl_eur=None, return_pct=None)
        equity = stats.get("equity_eur")
        stats["net_pnl_eur"] = (
            str(Decimal(equity) - Decimal(stats["initial_eur"])) if equity is not None else None
        )
        trades = portfolio["trades"]
        all_trades = trades + list(portfolio["positions"].values())
        stats["fees_eur"] = str(
            sum(
                (
                    Decimal(t["entry_fee_eur"]) + Decimal(t.get("exit_fee_eur") or "0")
                    for t in all_trades
                ),
                Decimal(0),
            )
        )
        closed_fees = sum(
            (Decimal(t["entry_fee_eur"]) + Decimal(t["exit_fee_eur"]) for t in trades),
            Decimal(0),
        )
        stats["closed_fees_eur"] = str(closed_fees)
        stats["closed_gross_eur"] = str(
            sum((Decimal(t["pnl_eur"]) for t in trades), Decimal(0)) + closed_fees
        )
        profit = sum((max(Decimal(t["pnl_eur"]), Decimal(0)) for t in trades), Decimal(0))
        loss = -sum((min(Decimal(t["pnl_eur"]), Decimal(0)) for t in trades), Decimal(0))
        stats["profit_factor"] = str(profit / loss) if loss else None
        stats["win_rate_pct"] = str(Decimal(stats["wins"]) / len(trades) * 100) if trades else None
        peak, drawdown = Decimal(stats["initial_eur"]), Decimal(0)
        for point in portfolio["equity"]:
            if point["equity_eur"] is not None:
                value = Decimal(point["equity_eur"])
                peak = max(peak, value)
                drawdown = max(drawdown, (peak - value) / peak * 100)
        stats["max_drawdown_pct"] = str(drawdown)
        strategies.append(
            {
                **definition,
                "config": portfolio["config"],
                "started_at": portfolio["started_at"],
                "last_cycle_at": portfolio["last_cycle_at"],
                "stats": stats,
                "state": "waiting_data" if stats["waiting_positions"] else result["state"],
                "positions": portfolio["positions"],
                "pending_orders": portfolio.get("pending_orders", {}),
                "maker_orders": portfolio.get("maker_orders", [])[-100:],
                "model_limitations": portfolio.get("model_limitations"),
                "trades": trades[-100:],
                "decisions": portfolio["decisions"][-60:],
                "equity": portfolio["equity"][-2000:],
                "market_signals": saved.get("market_signals", {}).get(definition["id"], []),
                "rank": None,
            }
        )
    strategies.sort(
        key=lambda s: (
            s["stats"]["return_pct"] is None,
            -Decimal(s["stats"]["return_pct"] or "0"),
            s["id"],
        )
    )
    previous, rank = None, 0
    for index, strategy in enumerate(strategies, 1):
        value = strategy["stats"]["return_pct"]
        if value is not None and not stale and not error:
            if previous is None or Decimal(value) != previous:
                rank = index
            strategy["rank"] = rank
            previous = Decimal(value)
    result["strategies"] = strategies
    return result


class ExperimentsService:
    name = "Comparación original"
    identity_prefix = "comparison"
    signal_version = "experiment-v1"

    def __init__(
        self, repository: SimulationRepository, feed: AnalysisFeed, *, clock: Clock | None = None
    ) -> None:
        self.repository, self.feed = repository, feed
        self.clock = clock or LiveClock()
        self.state: dict[str, Any] | None = None
        self.last_error: str | None = None

    def _catalog(self) -> list[dict[str, Any]]:
        return [d for d in definitions() if d["id"] != "cash"]

    def _engine(
        self, definition: dict[str, Any], state: dict[str, Any] | None, now: datetime
    ) -> SimulationEngine:
        return SimulationEngine(
            SimulationConfig.model_validate(definition["simulation_config"]), state, now
        )

    def _policy(self, ident: str, bars: list[Bar], now: datetime) -> dict[str, Any]:
        return evaluate(ident, bars, now)

    def _stable_policy(
        self, policy: dict[str, Any], cursor: dict[str, Any]
    ) -> dict[str, Any]:
        return policy

    @staticmethod
    def _holdings(portfolio: dict[str, Any]) -> dict[str, Any]:
        return {**portfolio.get("pending_orders", {}), **portfolio["positions"]}

    async def start(self) -> None:
        if self.state is not None:
            return
        catalog = self._catalog()
        saved = await self.repository.load()
        if saved is not None:
            if saved.get("version") != 1 or saved.get("definitions") != catalog:
                raise ValueError(
                    "La comparación guardada tiene otra versión de reglas; "
                    "no se reinicia su capital"
                )
            for definition in catalog:
                self._engine(definition, saved["portfolios"][definition["id"]], self.clock.now())
            self.state = saved
            return
        now = self.clock.now()
        saved = {
            "version": 1,
            "name": self.name,
            "experiment_id": self.identity_prefix + "-"
            + hashlib.sha256(now.isoformat().encode()).hexdigest()[:16],
            "definitions": catalog,
            "started_at": now.isoformat(),
            "last_cycle_at": None,
            "cycles": 0,
            "analysis_cycle": -1,
            "market_count": 0,
            "portfolios": {},
            "policies": {},
            "opportunities": {},
            "market_signals": {},
            "universe_history": [],
        }
        for definition in catalog:
            ident = definition["id"]
            saved["portfolios"][ident] = self._engine(definition, None, now).state
            saved["policies"][ident] = {}
            saved["opportunities"][ident] = {}
        await self.repository.save(saved)
        self.state = saved

    def holding_symbols(self) -> list[str]:
        return sorted(
            {
                symbol
                for portfolio in (self.state or {}).get("portfolios", {}).values()
                for symbol in self._holdings(portfolio)
            }
        )

    async def cycle(
        self,
        *,
        rows: list[dict[str, Any]],
        bars: dict[str, list[Bar]],
        books: dict[str, dict[str, Any]],
        markets: dict[str, dict[str, Any]],
        analysis_cycle: int,
    ) -> None:
        try:
            await self.start()
            assert self.state is not None
            if analysis_cycle <= self.state["analysis_cycle"]:
                return
            candidate = copy.deepcopy(self.state)
            now = self.clock.now()
            source_rows = {row["market"].split(":", 1)[1]: row for row in rows}
            previous_symbols = sorted({
                signal["symbol"]
                for signals in candidate["market_signals"].values()
                for signal in signals
            })
            current_symbols = sorted(source_rows)
            if previous_symbols != current_symbols:
                candidate.setdefault("universe_history", []).append({
                    "at": now.isoformat(),
                    "previous_symbols": previous_symbols,
                    "symbols": current_symbols,
                })
            symbols = sorted(set(source_rows) | set(self.holding_symbols()))
            prepared = {}
            needed = set(self.holding_symbols())
            for definition in candidate["definitions"]:
                ident = definition["id"]
                portfolio = candidate["portfolios"][ident]
                own_rows, opportunities, signals = [], [], []
                for symbol in symbols:
                    observation = source_rows.get(symbol, {})
                    policy = self._policy(ident, bars.get(symbol, []), now)
                    cursor = candidate["policies"][ident].setdefault(
                        symbol, {"armed": True, "bar_ts": None}
                    )
                    policy = self._stable_policy(policy, cursor)
                    bar_ts = policy.get("signal_bar_ts", policy.get("bar_ts"))
                    current_bar_ts = policy.get("current_bar_ts", bar_ts)
                    fresh = _fresh(current_bar_ts, now, 120)
                    new_bar = bool(
                        bar_ts
                        and (
                            cursor["bar_ts"] is None
                            or timestamp(bar_ts) > timestamp(cursor["bar_ts"])
                        )
                    )
                    reasons = _filters(observation, markets.get(symbol, {}), now)
                    if not fresh:
                        reasons.append("Velas ausentes o antiguas")
                    if not policy["ready"]:
                        reasons.append("Preparando indicadores o velas incompletas")
                    holding = self._holdings(portfolio).get(symbol)
                    cooldown = definition.get("cooldown_minutes", 0)
                    if not holding and cooldown:
                        last_closed = next(
                            (t for t in reversed(portfolio["trades"]) if t["symbol"] == symbol),
                            None,
                        )
                        if last_closed and now < timestamp(last_closed["closed_at"]) + timedelta(
                            minutes=cooldown
                        ):
                            reasons.append(f"Espera de {cooldown} minutos tras la última salida")
                    own = candidate["opportunities"][ident].get(symbol) if holding else None
                    if holding:
                        cursor["armed"] = False
                        if own is None:
                            raise ValueError("Posición sin señal propia persistida")
                        if not fresh or not policy["ready"] or symbol not in source_rows:
                            own["status"] = "interrupted"
                    elif new_bar and policy["ready"] and fresh and isinstance(bar_ts, str):
                        if not policy["enter"] or policy["exit"]:
                            cursor["armed"] = True
                        elif (
                            cursor["armed"]
                            and timestamp(bar_ts) >= timestamp(candidate["started_at"])
                            and not reasons
                        ):
                            digest = hashlib.sha256(
                                f"{candidate['experiment_id']}:{ident}:{symbol}:{bar_ts}".encode()
                            ).hexdigest()[:32]
                            # La prioridad de símbolos debe ser igual entre carteras
                            # cuando coinciden señales y solo quedan cinco plazas.
                            signal_id = f"{symbol}:{digest}"
                            own = {
                                "id": signal_id,
                                "market": f"revolutx:{symbol}",
                                "created_at": now.isoformat(),
                                "status": "open",
                                "rule_version": f"{self.signal_version}:{ident}",
                            }
                            candidate["opportunities"][ident][symbol] = own
                            cursor["armed"] = False
                            needed.add(symbol)
                    if new_bar:
                        cursor["bar_ts"] = bar_ts
                    signals.append({"symbol": symbol, **policy, "data_reasons": reasons})
                    if own is None:
                        continue
                    opportunities.append(own)
                    complete = [b for b in bars.get(symbol, []) if b.close_time <= now]
                    own_rows.append(
                        {
                            "market": own["market"],
                            "opportunity_id": own["id"],
                            "state": "open" if holding else "eligible",
                            "score": 0 if policy["exit"] else 100 if policy["enter"] else 50,
                            "quote_ts": observation.get("quote_ts"),
                            "bar_ts": current_bar_ts,
                            "signal_bar_ts": bar_ts,
                            "price": complete[-1].close if complete else None,
                            "atr": policy.get("atr"),
                            "reasons": reasons,
                            "exit_reasons": reasons,
                            "strategy_entry_reason": policy["entry_reason"],
                            "strategy_exit_reason": policy["exit_reason"],
                        }
                    )
                prepared[ident] = (own_rows, opportunities)
                candidate["market_signals"][ident] = signals
            # Una observación de profundidad para todas las carteras; ninguna consume
            # artificialmente la liquidez ni obtiene ventaja por ejecutarse primero.
            current_books = {}
            for symbol in sorted(needed):
                try:
                    book = books.get(symbol)
                    if book is None or not _fresh(book.get("ts"), self.clock.now(), 5):
                        book = await self.feed.book(symbol)
                    current_books[symbol] = book
                    books[symbol] = book
                except Exception:
                    continue
            executed_at = self.clock.now()
            for definition in candidate["definitions"]:
                ident = definition["id"]
                engine = self._engine(definition, candidate["portfolios"][ident], executed_at)
                own_rows, opportunities = prepared[ident]
                portfolio = engine.step(
                    own_rows, opportunities, current_books, markets, executed_at
                )
                candidate["portfolios"][ident] = portfolio
                candidate["opportunities"][ident] = {
                    symbol: opportunity
                    for symbol, opportunity in candidate["opportunities"][ident].items()
                    if symbol in self._holdings(portfolio)
                }
            candidate.update(
                last_cycle_at=executed_at.isoformat(),
                analysis_cycle=analysis_cycle,
                cycles=candidate["cycles"] + 1,
                market_count=len(rows),
            )
            await self.repository.save(candidate)
            self.state = candidate
            self.last_error = None
        except Exception as exc:
            self.last_error = f"Comparación pendiente de recuperar: {type(exc).__name__}"

    def status(self, running: bool) -> dict[str, Any]:
        return present(self.state, running, self.last_error, self.clock.now())
