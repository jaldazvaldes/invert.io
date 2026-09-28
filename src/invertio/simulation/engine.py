"""Motor puro: decisiones causales, contabilidad Decimal y precios del libro actual.

Este módulo no tiene cliente, red, reloj global, broker ni dependencias de órdenes.
El propietario persiste el snapshot completo después de cada ciclo de análisis.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation, localcontext
from typing import Any

from invertio.simulation.config import SimulationConfig

ZERO = Decimal(0)
HUNDRED = Decimal(100)


def _decimal(value: Any, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Dato numérico ausente o inválido")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Dato numérico inválido") from None
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise ValueError("Dato numérico no positivo o no finito")
    return result


def _text(value: Decimal) -> str:
    return format(value, "f")


def _time(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ValueError("La fecha debe incluir zona horaria")
    return result.astimezone(UTC)


def _fresh(value: Any, now: datetime, max_age: int) -> bool:
    try:
        return 0 <= (now - _time(value)).total_seconds() <= max_age
    except (TypeError, ValueError):
        return False


def _symbol(row: dict[str, Any]) -> str:
    value = row.get("market", row.get("symbol", ""))
    if not isinstance(value, str):
        return ""
    if ":" in value:
        venue, value = value.split(":", 1)
        if venue != "revolutx":
            return ""
    parts = value.split("/")
    return value if len(parts) == 2 and parts[0] and parts[1] == "EUR" else ""


def _book(
    raw: dict[str, Any] | None,
    now: datetime,
    max_age: int,
) -> tuple[list[tuple[Decimal, Decimal]], list[tuple[Decimal, Decimal]]]:
    if raw is None or not _fresh(raw.get("ts"), now, max_age):
        raise ValueError("Libro ausente, antiguo o con fecha futura")
    sides = []
    for name in ("bids", "asks"):
        values = raw.get(name)
        if not isinstance(values, list) or not values:
            raise ValueError("Libro sin profundidad suficiente")
        levels = []
        for value in values:
            if not isinstance(value, (list, tuple)) or len(value) < 2:
                raise ValueError("Niveles del libro inválidos")
            levels.append((_decimal(value[0], positive=True), _decimal(value[1], positive=True)))
        sides.append(sorted(levels, reverse=name == "bids"))
    bids, asks = sides
    if bids[0][0] > asks[0][0]:
        raise ValueError("Libro cruzado")
    return bids, asks


def _fill(levels: list[tuple[Decimal, Decimal]], quantity: Decimal) -> Decimal:
    remaining, value = quantity, ZERO
    for price, available in levels:
        amount = min(remaining, available)
        value += amount * price
        remaining -= amount
        if remaining == 0:
            return value
    raise ValueError("Profundidad insuficiente para la cantidad completa")


def _quantity_for_budget(levels: list[tuple[Decimal, Decimal]], budget: Decimal) -> Decimal:
    remaining, quantity = budget, ZERO
    for price, available in levels:
        spent = min(remaining, price * available)
        quantity += spent / price
        remaining -= spent
        if remaining == 0:
            return quantity
    raise ValueError("Profundidad insuficiente para la compra completa")


def _rules(market: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal | None, Decimal]:
    if market.get("active") is not True or not market.get("spot") or market.get("quote") != "EUR":
        raise ValueError("Mercado no activo o fuera del contado EUR")
    info, precision, limits = (
        market.get("info") or {},
        market.get("precision") or {},
        market.get("limits") or {},
    )
    amount_limits, cost_limits = limits.get("amount") or {}, limits.get("cost") or {}
    # Revolut X usa TICK_SIZE: precision.amount es un paso, no un número de decimales.
    step = _decimal(info.get("base_step", precision.get("amount")), positive=True)
    minimum = _decimal(info.get("min_order_size", amount_limits.get("min")) or 0)
    maximum_raw = info.get("max_order_size", amount_limits.get("max"))
    maximum = _decimal(maximum_raw, positive=True) if maximum_raw is not None else None
    cost_minimum = _decimal(info.get("min_order_size_quote", cost_limits.get("min")) or 0)
    return step, minimum, maximum, cost_minimum


class SimulationEngine:
    def __init__(
        self,
        config: SimulationConfig,
        state: dict[str, Any] | None,
        now: datetime,
    ) -> None:
        self.config = config
        now = _time(now)
        snapshot = config.model_dump(mode="json")
        if state is not None:
            if state.get("version") != 1 or state.get("config") != snapshot:
                raise ValueError("La versión o configuración de la simulación guardada no coincide")
            self.state = copy.deepcopy(state)
            _decimal(self.state["cash_eur"])
            _time(self.state["started_at"])
            if not isinstance(self.state["positions"], dict):
                raise ValueError("Posiciones guardadas inválidas")
            for key in ("trades", "decisions", "equity", "seen_opportunity_ids"):
                if not isinstance(self.state[key], list):
                    raise ValueError("Historial de simulación inválido")
        else:
            self.state = {
                "version": 1,
                "config": snapshot,
                "hypothetical": True,
                "started_at": now.isoformat(),
                "last_cycle_at": None,
                "cash_eur": _text(config.initial_eur),
                "positions": {},
                "trades": [],
                "decisions": [],
                "equity": [],
                "seen_opportunity_ids": [],
                "stats": {},
            }
            self._stats(config.initial_eur, ZERO)

    def holding_symbols(self) -> list[str]:
        return sorted(self.state["positions"])

    def _decision(self, ident: str, symbol: str, action: str, reason: str, now: datetime) -> None:
        self.state["decisions"].append(
            {
                "at": now.isoformat(),
                "opportunity_id": ident,
                "symbol": symbol,
                "action": action,
                "reason": reason,
            }
        )

    def _waiting(self, trade: dict[str, Any], reason: str, now: datetime) -> None:
        if trade["status"] != "waiting_data":
            self._decision(trade["id"], trade["symbol"], "wait", reason, now)
        trade.update(status="waiting_data", had_data_gap=True, reason=reason)

    def _sell(self, trade: dict[str, Any], proceeds: Decimal, reason: str, now: datetime) -> None:
        quantity = _decimal(trade["quantity"], positive=True)
        fee = proceeds * self.config.fee_pct / HUNDRED
        net = proceeds - fee
        pnl = net - _decimal(trade["investment_eur"])
        self.state["cash_eur"] = _text(_decimal(self.state["cash_eur"]) + net)
        trade.update(
            status="closed",
            closed_at=now.isoformat(),
            exit_price=_text(proceeds / quantity),
            exit_fee_eur=_text(fee),
            pnl_eur=_text(pnl),
            reason=reason,
        )
        self.state["trades"].append(trade)
        del self.state["positions"][trade["symbol"]]
        self._decision(trade["id"], trade["symbol"], "sell", reason, now)

    def _exit_reason(
        self,
        trade: dict[str, Any],
        row: dict[str, Any],
        opportunity: dict[str, Any],
        mid: Decimal,
        now: datetime,
    ) -> str | None:
        if mid <= _decimal(trade["stop"], positive=True):
            return "Stop observado; venta al libro actual"
        if mid >= _decimal(trade["target"], positive=True):
            return "Objetivo observado; venta al libro actual"
        if (
            row.get("score") is not None
            and _fresh(row.get("bar_ts"), now, self.config.bar_max_age_seconds)
            and _decimal(row["score"]) <= self.config.exit_score
        ):
            return str(row.get("strategy_exit_reason") or "Puntuación de salida")
        if now >= _time(trade["opened_at"]) + timedelta(minutes=self.config.lifetime_minutes):
            return "Caducidad de la simulación"
        if opportunity.get("status") == "closed":
            return f"Fin de señal: {opportunity.get('outcome') or 'cerrada'}"
        # El análisis separa los vetos de una nueva entrada de los filtros para
        # seguir la oportunidad original. Un ATR nuevo no cambia su objetivo.
        reasons = row.get("reasons", [])
        if row.get("opportunity_id") == trade["id"] and "exit_reasons" in row:
            reasons = row["exit_reasons"]
        filters = [
            str(reason)
            for reason in reasons
            if not str(reason).startswith("Nota inferior")
            and "no acredita volumen" not in str(reason)
            and "sin operaciones" not in str(reason).lower()
        ]
        if filters:
            return "Filtros de la señal: " + "; ".join(filters)
        return None

    def _entry(
        self,
        opportunity: dict[str, Any],
        symbol: str,
        row: dict[str, Any],
        raw_book: dict[str, Any] | None,
        market: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        if opportunity.get("status") != "open":
            raise ValueError("La señal ya no está abierta")
        created = _time(opportunity["created_at"])
        if not _time(self.state["started_at"]) <= created <= now:
            raise ValueError("Señal anterior al inicio o con fecha futura; no se reproduce")
        if not symbol:
            raise ValueError("Señal fuera de Revolut X EUR")
        if symbol in self.state["positions"]:
            raise ValueError("Ya hay una posición simulada en esta moneda")
        if len(self.state["positions"]) >= self.config.max_positions:
            raise ValueError("Sin plazas disponibles")
        if _decimal(self.state["cash_eur"]) < self.config.allocation_eur:
            raise ValueError("Efectivo insuficiente para la asignación completa")
        if (
            row.get("state") not in {"eligible", "open"}
            or _decimal(row.get("score")) < self.config.entry_score
            or row.get("reasons")
            or row.get("opportunity_id") not in {None, opportunity["id"]}
        ):
            raise ValueError("La fila actual no cumple las condiciones de entrada")
        if not _fresh(row.get("quote_ts"), now, self.config.quote_max_age_seconds):
            raise ValueError("Cotización de la señal antigua o ausente")
        if not _fresh(row.get("bar_ts"), now, self.config.bar_max_age_seconds):
            raise ValueError("Vela cerrada antigua, ausente o futura")
        step, minimum, maximum, cost_minimum = _rules(market)
        bids, asks = _book(raw_book, now, self.config.quote_max_age_seconds)
        mid = (bids[0][0] + asks[0][0]) / 2
        if (asks[0][0] - bids[0][0]) / mid * HUNDRED > self.config.max_spread_pct:
            raise ValueError("Spread superior al límite de entrada")
        fee_rate = self.config.fee_pct / HUNDRED
        budget = self.config.allocation_eur / (1 + fee_rate)
        raw_quantity = _quantity_for_budget(asks, budget)
        quantity = (raw_quantity / step).to_integral_value(rounding=ROUND_DOWN) * step
        if quantity <= 0 or quantity < minimum or (maximum is not None and quantity > maximum):
            raise ValueError("Cantidad incompatible con precisión o límites del mercado")
        gross = _fill(asks, quantity)
        if gross < cost_minimum:
            raise ValueError("Importe inferior al mínimo del mercado")
        entry_fee = gross * fee_rate
        investment = gross + entry_fee
        if investment > self.config.allocation_eur or investment > _decimal(self.state["cash_eur"]):
            raise ValueError("El coste con comisión supera la asignación")
        proceeds = _fill(bids, quantity)
        entry, sale = gross / quantity, proceeds / quantity
        if (entry / asks[0][0] - 1) * HUNDRED > self.config.max_slippage_pct:
            raise ValueError("Deslizamiento de compra superior al límite")
        if (1 - sale / bids[0][0]) * HUNDRED > self.config.max_slippage_pct:
            raise ValueError("Deslizamiento de venta superior al límite")
        atr = (
            _decimal(row["atr"], positive=True)
            if row.get("atr") is not None
            else _decimal(row.get("atr_pct"), positive=True)
            * _decimal(row.get("price"), positive=True)
            / HUNDRED
        )
        stop, target = entry - self.config.stop_atr * atr, entry + self.config.target_atr * atr
        if stop <= 0:
            raise ValueError("Stop calculado no positivo")
        if quantity * target * (sale / mid) * (1 - fee_rate) <= investment:
            raise ValueError("Objetivo neto no positivo después de costes")
        return {
            "id": opportunity["id"],
            "source_rule_version": opportunity.get("rule_version"),
            "source_created_at": created.isoformat(),
            "symbol": symbol,
            "status": "open",
            "opened_at": now.isoformat(),
            "closed_at": None,
            "entry": _text(entry),
            "quantity": _text(quantity),
            "stop": _text(stop),
            "target": _text(target),
            "entry_fee_eur": _text(entry_fee),
            "exit_fee_eur": None,
            "exit_price": None,
            "pnl_eur": None,
            "investment_eur": _text(investment),
            "gross_buy_eur": _text(gross),
            "fee_pct": _text(self.config.fee_pct),
            "reason": str(row.get("strategy_entry_reason") or "Entrada simulada al libro actual"),
            "had_data_gap": False,
        }

    def _stats(self, equity: Decimal | None, unrealized: Decimal | None) -> None:
        trades = self.state["trades"]
        realized = sum((Decimal(t["pnl_eur"]) for t in trades), ZERO)
        self.state["stats"] = {
            "initial_eur": _text(self.config.initial_eur),
            "cash_eur": self.state["cash_eur"],
            "equity_eur": _text(equity) if equity is not None else None,
            "realized_pnl_eur": _text(realized),
            "unrealized_pnl_eur": _text(unrealized) if unrealized is not None else None,
            "return_pct": _text((equity / self.config.initial_eur - 1) * HUNDRED)
            if equity is not None
            else None,
            "open_positions": len(self.state["positions"]),
            "waiting_positions": sum(
                t["status"] == "waiting_data" for t in self.state["positions"].values()
            ),
            "closed_trades": len(trades),
            "wins": sum(Decimal(t["pnl_eur"]) > 0 for t in trades),
            "losses": sum(Decimal(t["pnl_eur"]) < 0 for t in trades),
        }

    def step(
        self,
        rows: list[dict[str, Any]],
        opportunities: list[dict[str, Any]],
        books: dict[str, dict[str, Any]],
        markets: dict[str, dict[str, Any]],
        now: datetime,
    ) -> dict[str, Any]:
        now = _time(now)
        last = self.state.get("last_cycle_at")
        if now < _time(last or self.state["started_at"]):
            raise ValueError("El reloj de la simulación no puede retroceder")
        with localcontext() as context:
            context.prec = 50
            row_map = {_symbol(row): row for row in rows}
            opportunity_map = {op["id"]: op for op in opportunities}
            if last and (now - _time(last)).total_seconds() > self.config.bar_max_age_seconds:
                for trade in self.state["positions"].values():
                    self._waiting(trade, "Seguimiento interrumpido entre ciclos", now)
            for symbol, trade in list(self.state["positions"].items()):
                waiting = trade["status"] == "waiting_data"
                opportunity = opportunity_map.get(trade["id"], {})
                if not waiting and opportunity.get("status") == "interrupted":
                    self._waiting(
                        trade, "La señal se ha interrumpido; se espera una cotización nueva", now
                    )
                    waiting = True
                try:
                    bids, asks = _book(books.get(symbol), now, self.config.quote_max_age_seconds)
                    proceeds = _fill(bids, _decimal(trade["quantity"], positive=True))
                except (ValueError, TypeError, InvalidOperation):
                    self._waiting(
                        trade, "Sin libro reciente o profundidad suficiente para vender", now
                    )
                    continue
                reason = (
                    "Recuperación tras falta de datos"
                    if waiting
                    else self._exit_reason(
                        trade,
                        row_map.get(symbol, {}),
                        opportunity,
                        (bids[0][0] + asks[0][0]) / 2,
                        now,
                    )
                )
                if reason:
                    self._sell(trade, proceeds, reason, now)
            seen = set(self.state["seen_opportunity_ids"])
            for opportunity in sorted(
                opportunities, key=lambda op: (op.get("created_at", ""), op["id"])
            ):
                ident = opportunity["id"]
                if ident in seen:
                    continue
                symbol = _symbol(opportunity)
                seen.add(ident)
                self.state["seen_opportunity_ids"].append(ident)
                try:
                    historical = _time(opportunity["created_at"]) < _time(self.state["started_at"])
                except (ValueError, TypeError, KeyError):
                    historical = False
                if historical:
                    continue
                try:
                    trade = self._entry(
                        opportunity,
                        symbol,
                        row_map.get(symbol, {}),
                        books.get(symbol),
                        markets.get(symbol, {}),
                        now,
                    )
                except (ValueError, TypeError, KeyError, InvalidOperation) as error:
                    self._decision(ident, symbol, "skip", str(error), now)
                    continue
                self.state["cash_eur"] = _text(
                    _decimal(self.state["cash_eur"]) - _decimal(trade["investment_eur"])
                )
                self.state["positions"][symbol] = trade
                self._decision(ident, symbol, "buy", trade["reason"], now)
            equity: Decimal | None = _decimal(self.state["cash_eur"])
            unrealized: Decimal | None = ZERO
            for symbol, trade in self.state["positions"].items():
                if trade["status"] == "waiting_data":
                    equity = unrealized = None
                    break
                try:
                    bids, _ = _book(books.get(symbol), now, self.config.quote_max_age_seconds)
                    net = _fill(bids, _decimal(trade["quantity"], positive=True)) * (
                        1 - self.config.fee_pct / HUNDRED
                    )
                except (ValueError, TypeError, InvalidOperation):
                    equity = unrealized = None
                    break
                assert equity is not None and unrealized is not None
                equity += net
                unrealized += net - _decimal(trade["investment_eur"])
            self._stats(equity, unrealized)
            point = {
                "at": now.isoformat(),
                "cash_eur": self.state["cash_eur"],
                "equity_eur": _text(equity) if equity is not None else None,
            }
            if self.state["equity"] and self.state["equity"][-1]["at"] == point["at"]:
                self.state["equity"][-1] = point
            else:
                self.state["equity"].append(point)
            self.state["last_cycle_at"] = now.isoformat()
        return self.state
