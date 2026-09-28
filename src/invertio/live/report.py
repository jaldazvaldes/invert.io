"""Informe del modo paper: resultados por mercado y comparación cripto vs acciones."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from invertio.backtest.metrics import max_drawdown_pct
from invertio.config.app_config import AppConfig
from invertio.config.live_config import LiveConfig
from invertio.core.bus import EventBus
from invertio.core.models import TradingMode
from invertio.persistence.models import EquitySnapshotRow, FillRow, OrderRow
from invertio.persistence.recorder import fill_from_row, request_from_row
from invertio.portfolio import Portfolio


@dataclass(frozen=True, slots=True)
class MarketSummary:
    venue: str
    symbol: str
    currency: str
    trades: int
    wins: int
    pnl: Decimal
    fees: Decimal
    open_position: bool

    @property
    def win_rate_pct(self) -> float:
        return self.wins / self.trades * 100 if self.trades else 0.0


@dataclass(frozen=True, slots=True)
class VenueSummary:
    venue: str
    currency: str
    initial: Decimal
    equity: Decimal
    max_drawdown_pct: float
    first: datetime | None
    last: datetime | None

    @property
    def return_pct(self) -> float:
        return float(self.equity / self.initial - 1) * 100 if self.initial else 0.0


async def load_paper_portfolio(
    sessions: async_sessionmaker[AsyncSession], config: AppConfig, live: LiveConfig
) -> tuple[Portfolio, list[EquitySnapshotRow]]:
    """Reconstruye el portfolio de paper desde la base de datos (sin motor arrancado)."""
    mode = TradingMode.PAPER.value
    async with sessions() as session:
        orders = (await session.scalars(select(OrderRow).where(OrderRow.mode == mode))).all()
        fills = (
            await session.scalars(
                select(FillRow)
                .where(FillRow.mode == mode)
                .order_by(FillRow.ts, literal_column("fills.rowid"))
            )
        ).all()
        snapshots = (
            await session.scalars(
                select(EquitySnapshotRow)
                .where(EquitySnapshotRow.mode == mode)
                .order_by(EquitySnapshotRow.ts)
            )
        ).all()

    venues = {m.venue for m in live.markets}
    portfolio = Portfolio(
        EventBus(),
        {v: live.initial_cash[v] for v in venues},
        {v: config.venue(v).quote_currency for v in venues},
    )
    portfolio.replay(
        {row.client_order_id: request_from_row(row) for row in orders},
        [fill_from_row(row) for row in fills if row.venue in venues],
    )
    return portfolio, list(snapshots)


async def paper_summary(
    sessions: async_sessionmaker[AsyncSession], config: AppConfig, live: LiveConfig
) -> tuple[list[VenueSummary], list[MarketSummary]]:
    portfolio, snapshots = await load_paper_portfolio(sessions, config, live)
    venues = {m.venue for m in live.markets}

    markets = []
    for entry in live.markets:
        trades = [
            t for t in portfolio.closed_trades if (t.venue, t.symbol) == (entry.venue, entry.symbol)
        ]
        markets.append(
            MarketSummary(
                venue=entry.venue,
                symbol=entry.symbol,
                currency=config.venue(entry.venue).quote_currency,
                trades=len(trades),
                wins=sum(1 for t in trades if t.is_win),
                pnl=sum((t.pnl for t in trades), Decimal(0)),
                fees=sum((t.fees for t in trades), Decimal(0)),
                open_position=portfolio.position(entry.venue, entry.symbol).is_open,
            )
        )

    venue_summaries = []
    for venue in sorted(venues):
        curve = [s for s in snapshots if s.venue == venue]
        venue_summaries.append(
            VenueSummary(
                venue=venue,
                currency=config.venue(venue).quote_currency,
                initial=live.initial_cash[venue],
                equity=curve[-1].equity if curve else live.initial_cash[venue],
                max_drawdown_pct=max_drawdown_pct([s.equity for s in curve]),
                first=curve[0].ts if curve else None,
                last=curve[-1].ts if curve else None,
            )
        )
    return venue_summaries, markets
