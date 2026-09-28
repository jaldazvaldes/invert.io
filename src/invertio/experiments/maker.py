"""Modelo causal de compras pasivas; no envía órdenes ni presupone una cola real.

Una compra pendiente solo se considera ejecutada si un libro posterior ofrece
su cantidad completa estrictamente por debajo del límite original. Es una
aproximación conservadora con fotos del libro, no evidencia de una ejecución.
Las ventas usan la liquidación taker del motor de simulación existente.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation, localcontext
from typing import Any

from invertio.simulation.config import SimulationConfig
from invertio.simulation.engine import (
    HUNDRED,
    ZERO,
    SimulationEngine,
    _book,
    _decimal,
    _fill,
    _fresh,
    _rules,
    _symbol,
    _text,
    _time,
)

MODEL_LIMITATIONS = (
    "Modelo hipotético con libros observados: exige profundidad completa estrictamente "
    "por debajo del límite en una observación posterior. No reconstruye prioridad en "
    "cola, operaciones entre muestras ni ejecuciones parciales; puede omitir ejecuciones "
    "reales. Las compras modeladas pagan 0 % maker; las ventas mantienen comisión taker."
)


def _price_step(market: dict[str, Any]) -> Decimal:
    return _decimal(
        (market.get("info") or {}).get("quote_step", (market.get("precision") or {}).get("price")),
        positive=True,
    )


class MakerSimulationEngine(SimulationEngine):
    """Cuenta aislada con efectivo reservado y compras pasivas pendientes."""

    def __init__(
        self,
        config: SimulationConfig,
        state: dict[str, Any] | None,
        now: datetime,
        *,
        pending_minutes: int = 5,
    ) -> None:
        if isinstance(pending_minutes, bool) or pending_minutes not in {5, 15}:
            raise ValueError("La caducidad maker debe ser de 5 o 15 minutos")
        self.pending_minutes = pending_minutes
        model = {"version": "passive-buy-trade-through-v1", "pending_minutes": pending_minutes}
        if state is not None and state.get("maker_model") != model:
            raise ValueError("La configuración del modelo maker guardado no coincide")
        super().__init__(config, state, now)
        if state is None:
            self.state.update(
                maker_model=model,
                pending_orders={},
                maker_orders=[],
                model_limitations=MODEL_LIMITATIONS,
            )
            self._stats(config.initial_eur, ZERO)
        elif not isinstance(self.state.get("pending_orders"), dict) or not isinstance(
            self.state.get("maker_orders"), list
        ):
            raise ValueError("Órdenes maker guardadas inválidas")
        for symbol, pending in self.state["pending_orders"].items():
            if (
                symbol in self.state["positions"]
                or pending.get("symbol") != symbol
                or pending.get("status") != "pending"
                or pending.get("id") not in self.state["seen_opportunity_ids"]
                or _decimal(pending["reserved_eur"], positive=True) != config.allocation_eur
                or _decimal(pending["investment_eur"], positive=True) > config.allocation_eur
                or _time(pending["expires_at"]) <= _time(pending["placed_at"])
            ):
                raise ValueError("Reserva maker guardada inválida")

    def holding_symbols(self) -> list[str]:
        return sorted(set(self.state["positions"]) | set(self.state["pending_orders"]))

    def _reserved(self) -> Decimal:
        return sum(
            (
                _decimal(order["reserved_eur"])
                for order in self.state.get("pending_orders", {}).values()
            ),
            ZERO,
        )

    def _stats(self, equity: Decimal | None, unrealized: Decimal | None) -> None:
        reserved = self._reserved()
        super()._stats(equity + reserved if equity is not None else None, unrealized)
        orders = self.state.get("maker_orders", [])
        self.state["stats"].update(
            reserved_eur=_text(reserved),
            pending_orders=len(self.state.get("pending_orders", {})),
            maker_fills=sum(order["status"] == "filled" for order in orders),
            maker_expired=sum(order["status"] == "expired" for order in orders),
            maker_cancelled=sum(order["status"] == "cancelled" for order in orders),
        )

    def _finish_pending(
        self,
        order: dict[str, Any],
        status: str,
        reason: str,
        now: datetime,
        *,
        incomplete: bool = False,
    ) -> None:
        self.state["cash_eur"] = _text(
            _decimal(self.state["cash_eur"]) + _decimal(order["reserved_eur"])
        )
        del self.state["pending_orders"][order["symbol"]]
        order.update(
            status=status, finished_at=now.isoformat(), reason=reason, incomplete=incomplete
        )
        self.state["maker_orders"].append(order)
        self._decision(order["id"], order["symbol"], status, reason, now)

    def _validate_quantity(self, quantity: Decimal, price: Decimal, market: dict[str, Any]) -> None:
        step, minimum, maximum, cost_minimum = _rules(market)
        if (
            quantity <= 0
            or quantity % step != 0
            or quantity < minimum
            or (maximum is not None and quantity > maximum)
            or price % _price_step(market) != 0
        ):
            raise ValueError("Cantidad o precio incompatible con los límites del mercado")
        if price * quantity < cost_minimum:
            raise ValueError("Importe inferior al mínimo del mercado")

    def _validate_costs(
        self,
        bids: list[tuple[Decimal, Decimal]],
        asks: list[tuple[Decimal, Decimal]],
        quantity: Decimal,
        investment: Decimal,
        target: Decimal,
    ) -> None:
        mid = (bids[0][0] + asks[0][0]) / 2
        if (asks[0][0] - bids[0][0]) / mid * HUNDRED > self.config.max_spread_pct:
            raise ValueError("Spread superior al límite de entrada")
        sale = _fill(bids, quantity) / quantity
        if (1 - sale / bids[0][0]) * HUNDRED > self.config.max_slippage_pct:
            raise ValueError("Deslizamiento de venta superior al límite")
        if quantity * target * (sale / mid) * (1 - self.config.fee_pct / HUNDRED) <= investment:
            raise ValueError("Objetivo neto no positivo después de costes")

    def _place(
        self,
        opportunity: dict[str, Any],
        symbol: str,
        row: dict[str, Any],
        raw_book: dict[str, Any] | None,
        market: dict[str, Any],
        now: datetime,
    ) -> None:
        created = _time(opportunity["created_at"])
        if (
            opportunity.get("status") != "open"
            or not _time(self.state["started_at"]) <= created <= now
        ):
            raise ValueError("Señal cerrada, histórica o con fecha futura")
        if not symbol or symbol in self.holding_symbols():
            raise ValueError("Mercado inválido o ya tiene una posición o reserva")
        if len(self.holding_symbols()) >= self.config.max_positions:
            raise ValueError("Sin plazas disponibles: las órdenes pendientes ocupan plaza")
        if _decimal(self.state["cash_eur"]) < self.config.allocation_eur:
            raise ValueError("Efectivo insuficiente para la reserva completa")
        if (
            row.get("state") not in {"eligible", "open"}
            or _decimal(row.get("score")) < self.config.entry_score
            or row.get("reasons")
            or row.get("opportunity_id") not in {None, opportunity["id"]}
        ):
            raise ValueError("La fila actual no cumple las condiciones de entrada")
        if not _fresh(row.get("quote_ts"), now, self.config.quote_max_age_seconds) or not _fresh(
            row.get("bar_ts"), now, self.config.bar_max_age_seconds
        ):
            raise ValueError("Cotización o vela antigua, ausente o futura")
        bids, asks = _book(raw_book, now, self.config.quote_max_age_seconds)
        step, _, _, _ = _rules(market)
        price_step = _price_step(market)
        price = (bids[0][0] / price_step).to_integral_value(rounding=ROUND_DOWN) * price_step
        if price <= 0 or price >= asks[0][0]:
            raise ValueError("El límite no es pasivo: alcanzaría la mejor venta")
        quantity = (self.config.allocation_eur / price / step).to_integral_value(
            rounding=ROUND_DOWN
        ) * step
        self._validate_quantity(quantity, price, market)
        investment = price * quantity
        atr = (
            _decimal(row["atr"], positive=True)
            if row.get("atr") is not None
            else _decimal(row.get("atr_pct"), positive=True)
            * _decimal(row.get("price"), positive=True)
            / HUNDRED
        )
        stop, target = price - self.config.stop_atr * atr, price + self.config.target_atr * atr
        if stop <= 0:
            raise ValueError("Stop calculado no positivo")
        self._validate_costs(bids, asks, quantity, investment, target)
        assert raw_book is not None
        pending = {
            "id": opportunity["id"],
            "source_rule_version": opportunity.get("rule_version"),
            "source_created_at": created.isoformat(),
            "symbol": symbol,
            "status": "pending",
            "placed_at": now.isoformat(),
            "placed_book_ts": _time(raw_book["ts"]).isoformat(),
            "placed_best_bid": _text(bids[0][0]),
            "placed_best_ask": _text(asks[0][0]),
            "signal_bar_ts": row.get("signal_bar_ts", row.get("bar_ts")),
            "expires_at": (now + timedelta(minutes=self.pending_minutes)).isoformat(),
            "finished_at": None,
            "limit_price": _text(price),
            "quantity": _text(quantity),
            "reserved_eur": _text(self.config.allocation_eur),
            "investment_eur": _text(investment),
            "stop": _text(stop),
            "target": _text(target),
            "atr_at_placement": _text(atr),
            "entry_fee_eur": "0",
            "reason": str(
                row.get("strategy_entry_reason") or "Compra pasiva simulada al mejor bid"
            ),
            "incomplete": False,
        }
        self.state["pending_orders"][symbol] = pending
        self.state["cash_eur"] = _text(
            _decimal(self.state["cash_eur"]) - self.config.allocation_eur
        )
        self._decision(pending["id"], symbol, "place", pending["reason"], now)

    def _pending(
        self,
        order: dict[str, Any],
        row: dict[str, Any],
        opportunity: dict[str, Any],
        raw_book: dict[str, Any] | None,
        market: dict[str, Any],
        now: datetime,
        gap: bool,
    ) -> None:
        if now >= _time(order["expires_at"]):
            self._finish_pending(
                order,
                "expired",
                "Límite caducado tras interrupción; ejecución desconocida"
                if gap
                else "Límite caducado sin ejecución modelada",
                now,
                incomplete=gap,
            )
            return
        if gap:
            self._finish_pending(
                order,
                "cancelled",
                "Seguimiento maker interrumpido entre ciclos",
                now,
                incomplete=True,
            )
            return
        try:
            if not _fresh(
                row.get("quote_ts"), now, self.config.quote_max_age_seconds
            ) or not _fresh(row.get("bar_ts"), now, self.config.bar_max_age_seconds):
                raise ValueError("Cotización o vela antigua, ausente o futura")
            bids, asks = _book(raw_book, now, self.config.quote_max_age_seconds)
        except (ValueError, TypeError, InvalidOperation) as error:
            self._finish_pending(order, "cancelled", str(error), now, incomplete=True)
            return
        try:
            quantity, price = (
                _decimal(order["quantity"], positive=True),
                _decimal(order["limit_price"], positive=True),
            )
            self._validate_quantity(quantity, price, market)
        except (ValueError, TypeError, InvalidOperation) as error:
            self._finish_pending(order, "cancelled", str(error), now, incomplete=True)
            return
        assert raw_book is not None
        book_at = _time(raw_book["ts"])
        if (
            now <= _time(order["placed_at"])
            or book_at <= max(_time(order["placed_at"]), _time(order["placed_book_ts"]))
            or sum((size for level, size in asks if level < price), ZERO) < quantity
        ):
            # Solo una orden aún no ejecutada puede cancelarse por datos nuevos.
            # Evaluar esos vetos antes del cruce descartaría compras perdedoras.
            try:
                if opportunity.get("status") in {"closed", "interrupted"}:
                    raise ValueError("La señal asociada terminó o se interrumpió")
                if _decimal(row.get("score")) <= self.config.exit_score or row.get("reasons"):
                    raise ValueError(
                        str(
                            row.get("strategy_exit_reason")
                            or "Señal de salida o filtros incumplidos"
                        )
                    )
                self._validate_costs(
                    bids,
                    asks,
                    quantity,
                    _decimal(order["investment_eur"]),
                    _decimal(order["target"]),
                )
            except (ValueError, TypeError, InvalidOperation) as error:
                self._finish_pending(
                    order,
                    "cancelled",
                    str(error),
                    now,
                    incomplete=opportunity.get("status") == "interrupted",
                )
            return
        # El precio original se conserva aunque el libro nuevo permita comprar más barato.
        trade = {
            "id": order["id"],
            "source_rule_version": order["source_rule_version"],
            "source_created_at": order["source_created_at"],
            "symbol": order["symbol"],
            "status": "open",
            "opened_at": now.isoformat(),
            "closed_at": None,
            "entry": order["limit_price"],
            "quantity": order["quantity"],
            "stop": order["stop"],
            "target": order["target"],
            "entry_fee_eur": "0",
            "exit_fee_eur": None,
            "exit_price": None,
            "pnl_eur": None,
            "investment_eur": order["investment_eur"],
            "gross_buy_eur": order["investment_eur"],
            "fee_pct": _text(self.config.fee_pct),
            "entry_fee_pct": "0",
            "execution_model": "maker_model_trade_through",
            "entry_execution_model": "maker_model_trade_through",
            "maker_placed_at": order["placed_at"],
            "maker_fill_book_ts": book_at.isoformat(),
            "reason": "Compra maker modelada por libro posterior bajo el límite",
            "had_data_gap": False,
        }
        self.state["cash_eur"] = _text(
            _decimal(self.state["cash_eur"])
            + _decimal(order["reserved_eur"])
            - _decimal(order["investment_eur"])
        )
        del self.state["pending_orders"][order["symbol"]]
        self.state["positions"][order["symbol"]] = trade
        order.update(
            status="filled",
            finished_at=now.isoformat(),
            fill_book_ts=book_at.isoformat(),
            fill_best_bid=_text(bids[0][0]),
            fill_best_ask=_text(asks[0][0]),
            fill_depth_below_limit=_text(
                sum((size for level, size in asks if level < price), ZERO)
            ),
            execution_model="maker_model_trade_through",
            reason=trade["reason"],
        )
        self.state["maker_orders"].append(order)
        self._decision(order["id"], order["symbol"], "buy", trade["reason"], now)

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
            rows_by_symbol = {_symbol(row): row for row in rows}
            by_id = {op["id"]: op for op in opportunities}
            gap = bool(last and (now - _time(last)).total_seconds() > 120)
            for symbol, order in list(self.state["pending_orders"].items()):
                self._pending(
                    order,
                    rows_by_symbol.get(symbol, {}),
                    by_id.get(order["id"], {}),
                    books.get(symbol),
                    markets.get(symbol, {}),
                    now,
                    gap,
                )
            # Las ventas conservan la comisión y los controles del motor taker.
            seen = set(self.state["seen_opportunity_ids"])
            super().step(
                rows, [op for op in opportunities if op["id"] in seen], books, markets, now
            )
            for opportunity in sorted(
                opportunities, key=lambda op: (op.get("created_at", ""), op["id"])
            ):
                ident, symbol = opportunity["id"], _symbol(opportunity)
                if ident in seen:
                    continue
                seen.add(ident)
                self.state["seen_opportunity_ids"].append(ident)
                try:
                    if _time(opportunity["created_at"]) < _time(self.state["started_at"]):
                        continue
                    self._place(
                        opportunity,
                        symbol,
                        rows_by_symbol.get(symbol, {}),
                        books.get(symbol),
                        markets.get(symbol, {}),
                        now,
                    )
                except (ValueError, TypeError, KeyError, InvalidOperation) as error:
                    self._decision(ident, symbol, "skip", str(error), now)
            # Reservar efectivo cambia saldo libre, pero no el valor de la cuenta.
            stats = self.state["stats"]
            equity = Decimal(stats["equity_eur"]) if stats["equity_eur"] is not None else None
            unrealized = (
                Decimal(stats["unrealized_pnl_eur"])
                if stats["unrealized_pnl_eur"] is not None
                else None
            )
            self._stats(equity - self._reserved() if equity is not None else None, unrealized)
            self.state["equity"][-1].update(
                cash_eur=self.state["cash_eur"], equity_eur=self.state["stats"]["equity_eur"]
            )
        return self.state
