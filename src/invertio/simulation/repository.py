"""Un checkpoint atómico incluye efectivo, posiciones, decisiones y valoraciones."""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from invertio.persistence.simulation_models import SimulationStateRow

KEY = "auto-score-v1"


class SimulationRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], *, key: str = KEY) -> None:
        self.sessions = sessions
        self.key = key

    async def load(self) -> dict[str, Any] | None:
        async with self.sessions() as session:
            row = await session.get(SimulationStateRow, self.key)
            return copy.deepcopy(row.payload) if row else None

    async def save(self, snapshot: dict[str, Any]) -> None:
        async with self.sessions.begin() as session:
            statement = insert(SimulationStateRow).values(
                key=self.key, payload=copy.deepcopy(snapshot)
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[SimulationStateRow.key],
                    set_={"payload": statement.excluded.payload},
                )
            )
