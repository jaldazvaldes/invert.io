"""Grabación en SQLite de todo lo que pasa en vivo, y recuperación del estado al reiniciar.

Las escrituras se encolan y las hace una única tarea en segundo plano: la cadena
vela → señal → orden nunca espera al disco.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import delete, func, literal_column, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from invertio.core.bus import EventBus
from invertio.core.clock import Clock
from invertio.core.events import EngineStateChanged, OrderFilled, OrderUpdated, RiskDecision
from invertio.core.models import (
    Fill,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    TradingMode,
)
from invertio.persistence.models import (
    AuditLogRow,
    EquitySnapshotRow,
    FillRow,
    OrderRow,
    SignalRow,
)

log = structlog.get_logger(__name__)

type Write = Callable[[AsyncSession], Awaitable[None] | None]


class Recorder:
    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        sessions: async_sessionmaker[AsyncSession],
        mode: TradingMode,
    ) -> None:
        self._clock = clock
        self._sessions = sessions
        self._mode = mode.value
        self._queue: asyncio.Queue[Write | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        bus.subscribe(RiskDecision, self._on_decision)
        bus.subscribe(OrderUpdated, self._on_order)
        bus.subscribe(OrderFilled, self._on_fill)
        bus.subscribe(EngineStateChanged, self._on_state)

    # --- ciclo de vida -----------------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._writer(), name="recorder")

    async def stop(self) -> None:
        """Escribe lo pendiente y termina."""
        if self._task is None:
            return
        await self._queue.put(None)
        await self._task
        self._task = None

    async def flush(self) -> None:
        await self._queue.join()

    async def _writer(self) -> None:
        while True:
            write = await self._queue.get()
            try:
                if write is None:
                    return
                async with self._sessions() as session:
                    result = write(session)
                    if asyncio.iscoroutine(result):
                        await result
                    await session.commit()
            except Exception:
                log.exception("error guardando en la base de datos")
            finally:
                self._queue.task_done()

    # --- escrituras --------------------------------------------------------------------

    def snapshot_equity(
        self, ts: datetime, venue: str, currency: str, equity: Decimal, cash: Decimal
    ) -> None:
        row = EquitySnapshotRow(
            mode=self._mode, ts=ts, venue=venue, currency=currency, equity=equity, cash=cash
        )
        self._queue.put_nowait(lambda s: s.add(row))

    def audit(self, ts: datetime, level: str, event: str, data: dict[str, Any]) -> None:
        row = AuditLogRow(mode=self._mode, ts=ts, level=level, event=event, data=data)
        self._queue.put_nowait(lambda s: s.add(row))

    def _on_decision(self, event: RiskDecision) -> None:
        s = event.signal
        row = SignalRow(
            id=s.id,
            mode=self._mode,
            ts=s.ts,
            strategy_id=s.strategy_id,
            venue=s.venue,
            symbol=s.symbol,
            action=s.action.value,
            price=s.price,
            stop_loss=s.stop_loss,
            take_profit=s.take_profit,
            reason=s.reason,
            approved=event.approved,
            decision_reason=event.reason,
        )
        self._queue.put_nowait(lambda session: session.add(row))

    def _on_order(self, event: OrderUpdated) -> None:
        o, r = event.order, event.order.request
        row = OrderRow(
            client_order_id=o.client_order_id,
            mode=self._mode,
            venue=r.venue,
            symbol=r.symbol,
            side=r.side.value,
            type=r.type.value,
            quantity=r.quantity,
            limit_price=r.limit_price,
            post_only=r.post_only,
            stop_loss=r.stop_loss,
            take_profit=r.take_profit,
            reason=r.reason,
            strategy_id=r.strategy_id,
            signal_id=r.signal_id,
            venue_order_id=o.venue_order_id,
            status=o.status.value,
            filled_quantity=o.filled_quantity,
            avg_fill_price=o.avg_fill_price,
            reject_reason=o.reject_reason,
            created_at=o.created_at,
            updated_at=o.updated_at,
        )

        async def upsert(session: AsyncSession) -> None:
            await session.merge(row)

        self._queue.put_nowait(upsert)

    def _on_fill(self, event: OrderFilled) -> None:
        f = event.fill
        row = FillRow(
            id=f.id,
            client_order_id=f.client_order_id,
            mode=self._mode,
            venue=f.venue,
            symbol=f.symbol,
            side=f.side.value,
            quantity=f.quantity,
            price=f.price,
            fee=f.fee,
            fee_currency=f.fee_currency,
            ts=f.ts,
        )
        self._queue.put_nowait(lambda session: session.add(row))

    def _on_state(self, event: EngineStateChanged) -> None:
        self.audit(
            self._clock.now(), "warning", f"engine_{event.state.value}", {"reason": event.reason}
        )


# --- recuperación ------------------------------------------------------------------------


@dataclass(slots=True)
class RestoredState:
    requests: dict[str, OrderRequest] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    canceled_on_restart: int = 0
    last_snapshot: dict[str, datetime] = field(default_factory=dict)


def request_from_row(row: OrderRow) -> OrderRequest:
    return OrderRequest(
        venue=row.venue,
        symbol=row.symbol,
        side=Side(row.side),
        type=OrderType(row.type),
        quantity=row.quantity,
        strategy_id=row.strategy_id,
        limit_price=row.limit_price,
        post_only=row.post_only,
        stop_loss=row.stop_loss,
        take_profit=row.take_profit,
        signal_id=row.signal_id,
        reason=row.reason,
        client_order_id=row.client_order_id,
    )


def order_from_row(row: OrderRow) -> Order:
    return Order(
        request_from_row(row),
        created_at=row.created_at,
        status=OrderStatus(row.status),
        venue_order_id=row.venue_order_id,
        filled_quantity=row.filled_quantity,
        avg_fill_price=row.avg_fill_price,
        updated_at=row.updated_at,
        reject_reason=row.reject_reason,
    )


def fill_from_row(row: FillRow) -> Fill:
    return Fill(
        client_order_id=row.client_order_id,
        venue=row.venue,
        symbol=row.symbol,
        side=Side(row.side),
        quantity=row.quantity,
        price=row.price,
        fee=row.fee,
        fee_currency=row.fee_currency,
        ts=row.ts,
        id=row.id,
    )


async def load_state(
    sessions: async_sessionmaker[AsyncSession], mode: TradingMode, now: datetime
) -> RestoredState:
    """Lee el historial del modo indicado. Las órdenes que quedaron abiertas se cancelan:
    el simulador no puede saber qué habría pasado con ellas mientras estaba parado."""
    state = RestoredState()
    async with sessions() as session:
        orders = (await session.scalars(select(OrderRow).where(OrderRow.mode == mode.value))).all()
        state.requests = {row.client_order_id: request_from_row(row) for row in orders}
        open_ids = [
            row.client_order_id
            for row in orders
            if not OrderStatus(row.status).is_terminal and row.filled_quantity == 0
        ]
        if open_ids:
            await session.execute(
                update(OrderRow)
                .where(OrderRow.client_order_id.in_(open_ids))
                .values(status=OrderStatus.CANCELED.value, updated_at=now)
            )
            state.canceled_on_restart = len(open_ids)
        fills = await session.scalars(
            select(FillRow)
            .where(FillRow.mode == mode.value)
            .order_by(FillRow.ts, literal_column("fills.rowid"))
        )
        state.fills = [fill_from_row(row) for row in fills]
        snapshots = await session.execute(
            select(EquitySnapshotRow.venue, func.max(EquitySnapshotRow.ts))
            .where(EquitySnapshotRow.mode == mode.value)
            .group_by(EquitySnapshotRow.venue)
        )
        state.last_snapshot = {venue: ts for venue, ts in snapshots.all() if ts is not None}
        await session.commit()
    return state


async def last_equity_before(
    sessions: async_sessionmaker[AsyncSession], mode: TradingMode, before: datetime
) -> dict[str, Decimal]:
    """Última equity anotada de cada venue antes de `before` (p. ej. la medianoche local)."""
    result: dict[str, Decimal] = {}
    async with sessions() as session:
        venues = await session.scalars(
            select(EquitySnapshotRow.venue).where(EquitySnapshotRow.mode == mode.value).distinct()
        )
        for venue in venues.all():
            row = await session.scalar(
                select(EquitySnapshotRow)
                .where(
                    EquitySnapshotRow.mode == mode.value,
                    EquitySnapshotRow.venue == venue,
                    EquitySnapshotRow.ts < before,
                )
                .order_by(EquitySnapshotRow.ts.desc())
                .limit(1)
            )
            if row is not None:
                result[venue] = row.equity
    return result


async def reset_mode(sessions: async_sessionmaker[AsyncSession], mode: TradingMode) -> None:
    """Borra todo el historial de un modo (solo se permite para paper)."""
    if mode is not TradingMode.PAPER:
        raise ValueError("Solo se puede reiniciar el historial del modo paper")
    async with sessions() as session:
        for model in (FillRow, OrderRow, SignalRow, EquitySnapshotRow, AuditLogRow):
            await session.execute(delete(model).where(model.mode == mode.value))
        await session.commit()
