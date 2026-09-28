"""Transactional analysis snapshots and a durable notification outbox on SQLite."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from invertio.persistence.analysis_models import (
    AnalysisNotificationRow,
    AnalysisObservationRow,
    AnalysisOpportunityRow,
    AnalysisStateRow,
)


def _utc(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if result.tzinfo is None:
        raise ValueError("Analysis timestamps must include a timezone")
    return result.astimezone(UTC)


def _notification(row: AnalysisNotificationRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "opportunity_id": row.opportunity_id,
        "kind": row.kind,
        "created_at": row.created_at.isoformat(),
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "status": row.status,
        "attempts": row.attempts,
        "last_error": row.last_error,
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
        "text": row.text,
    }


class AnalysisRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def load_state(self) -> dict[str, dict[str, Any]]:
        async with self._sessions() as session:
            rows = (await session.scalars(select(AnalysisStateRow))).all()
            return {row.key: row.payload for row in rows}

    async def opportunities(
        self,
        status: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict[str, Any]]:
        statement = select(AnalysisOpportunityRow)
        if status is not None:
            statement = statement.where(AnalysisOpportunityRow.status == status)
        if since is not None:
            statement = statement.where(AnalysisOpportunityRow.created_at >= _utc(since))
        if until is not None:
            statement = statement.where(AnalysisOpportunityRow.created_at <= _utc(until))
        statement = statement.order_by(
            AnalysisOpportunityRow.created_at.desc(), AnalysisOpportunityRow.id.desc()
        )
        async with self._sessions() as session:
            return [row.payload for row in (await session.scalars(statement)).all()]

    async def opportunity(self, opportunity_id: str) -> dict[str, Any] | None:
        async with self._sessions() as session:
            row = await session.get(AnalysisOpportunityRow, opportunity_id)
            return row.payload if row else None

    async def observations(
        self,
        market: str | None = None,
        limit: int = 100,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        statement = select(AnalysisObservationRow)
        if market is not None:
            statement = statement.where(AnalysisObservationRow.market == market)
        if since is not None:
            statement = statement.where(AnalysisObservationRow.observed_at >= _utc(since))
        if until is not None:
            statement = statement.where(AnalysisObservationRow.observed_at <= _utc(until))
        statement = statement.order_by(
            AnalysisObservationRow.observed_at.desc(), AnalysisObservationRow.market
        ).limit(limit)
        async with self._sessions() as session:
            return [row.payload for row in (await session.scalars(statement)).all()]

    async def commit_cycle(
        self,
        *,
        observations: list[dict[str, Any]],
        opportunities: list[dict[str, Any]],
        states: dict[str, dict[str, Any]],
        notifications: list[dict[str, Any]],
    ) -> None:
        """Persist all changes or none. Replayed outbox entries never reset delivery state."""
        async with self._sessions.begin() as session:
            for observation in observations:
                statement = insert(AnalysisObservationRow).values(
                    market=observation["market"],
                    bar_ts=_utc(observation["bar_ts"]),
                    rule_version=observation["rule_version"],
                    observed_at=_utc(observation["observed_at"]),
                    payload=observation,
                )
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=["market", "bar_ts", "rule_version"],
                        set_={
                            "observed_at": statement.excluded.observed_at,
                            "payload": statement.excluded.payload,
                        },
                    )
                )
            # Close an existing opportunity before inserting its successor in this transaction.
            for opportunity in sorted(opportunities, key=lambda item: item["status"] == "open"):
                statement = insert(AnalysisOpportunityRow).values(
                    id=opportunity["id"],
                    market=opportunity["market"],
                    created_at=_utc(opportunity["created_at"]),
                    status=opportunity["status"],
                    payload=opportunity,
                )
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=["id"],
                        set_={
                            "status": statement.excluded.status,
                            "payload": statement.excluded.payload,
                        },
                    )
                )
            for key, payload in states.items():
                statement = insert(AnalysisStateRow).values(key=key, payload=payload)
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=["key"],
                        set_={"payload": statement.excluded.payload},
                    )
                )
            for notification in notifications:
                expected_id = f"{notification['opportunity_id']}:{notification['kind']}"
                if notification["id"] != expected_id:
                    raise ValueError("Notification id must be opportunity_id:kind")
                await session.execute(
                    insert(AnalysisNotificationRow)
                    .values(
                        id=notification["id"],
                        opportunity_id=notification["opportunity_id"],
                        kind=notification["kind"],
                        created_at=_utc(notification["created_at"]),
                        expires_at=_utc(notification["expires_at"])
                        if notification.get("expires_at")
                        else None,
                        text=notification["text"],
                        status="pending",
                        attempts=0,
                    )
                    .on_conflict_do_nothing()
                )

    async def notifications(self, opportunity_id: str) -> list[dict[str, Any]]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(AnalysisNotificationRow)
                    .where(
                        AnalysisNotificationRow.opportunity_id == opportunity_id,
                    )
                    .order_by(
                        AnalysisNotificationRow.created_at, AnalysisNotificationRow.kind.desc()
                    )
                )
            ).all()
            return [_notification(row) for row in rows]

    async def pending_notifications(self, now: datetime) -> list[dict[str, Any]]:
        """Include expired starts so the dispatcher can persist their expired state."""
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(AnalysisNotificationRow)
                    .where(
                        AnalysisNotificationRow.status.in_(["pending", "failed"]),
                        AnalysisNotificationRow.created_at <= _utc(now),
                    )
                    .order_by(
                        AnalysisNotificationRow.created_at, AnalysisNotificationRow.kind.desc()
                    )
                )
            ).all()
            return [_notification(row) for row in rows]

    async def mark_notification(
        self,
        notification_id: str,
        status: str,
        error: str | None = None,
        *,
        now: datetime | None = None,
    ) -> None:
        if status not in {"sent", "failed", "expired"}:
            raise ValueError("Invalid notification transition")
        values: dict[str, Any] = {"status": status, "last_error": error}
        if status in {"sent", "failed"}:
            values["attempts"] = AnalysisNotificationRow.attempts + 1
        if status == "sent":
            values["sent_at"] = _utc(now) if now else datetime.now(UTC)
        async with self._sessions.begin() as session:
            await session.execute(
                update(AnalysisNotificationRow)
                .where(
                    AnalysisNotificationRow.id == notification_id,
                    AnalysisNotificationRow.status.in_(["pending", "failed"]),
                )
                .values(**values)
            )

    async def notification_counts(self) -> dict[str, int]:
        counts = {"pending": 0, "sent": 0, "failed": 0, "expired": 0}
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(
                        AnalysisNotificationRow.status,
                        func.count(),
                    ).group_by(AnalysisNotificationRow.status)
                )
            ).all()
            counts.update({status: count for status, count in rows})
        return counts
