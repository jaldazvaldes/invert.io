"""Persistir antes de enviar: un reinicio nunca vuelve a enviar una orden."""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from invertio.persistence.manual_models import ManualOrderRow


class ManualRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def orders(self) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = (await session.scalars(select(ManualOrderRow))).all()
            return sorted(
                [copy.deepcopy(row.payload) for row in rows],
                key=lambda row: str(row["created_at"]),
                reverse=True,
            )

    async def save(self, order: dict[str, Any]) -> None:
        async with self.sessions.begin() as session:
            stmt = insert(ManualOrderRow).values(
                id=order["id"], status=order["status"], payload=copy.deepcopy(order)
            )
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[ManualOrderRow.id],
                    set_={"status": stmt.excluded.status, "payload": stmt.excluded.payload},
                )
            )
