"""Analysis-only storage; no trading mode, orders, fills or positions are involved."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from invertio.persistence.models import Base
from invertio.persistence.types import UTCDateTime


class AnalysisObservationRow(Base):
    __tablename__ = "analysis_observations"

    market: Mapped[str] = mapped_column(String(96), primary_key=True)
    bar_ts: Mapped[datetime] = mapped_column(UTCDateTime, primary_key=True)
    rule_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)

    __table_args__ = (Index("ix_analysis_observations_observed", "observed_at"),)


class AnalysisOpportunityRow(Base):
    __tablename__ = "analysis_opportunities"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    market: Mapped[str] = mapped_column(String(96))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)

    __table_args__ = (
        CheckConstraint("status IN ('open', 'closed', 'interrupted')"),
        Index("ix_analysis_opportunities_created", "created_at"),
        Index(
            "uq_analysis_open_market",
            "market",
            unique=True,
            sqlite_where=text("status = 'open'"),
        ),
    )


class AnalysisStateRow(Base):
    __tablename__ = "analysis_state"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class AnalysisNotificationRow(Base):
    __tablename__ = "analysis_notifications"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    opportunity_id: Mapped[str] = mapped_column(ForeignKey("analysis_opportunities.id"))
    kind: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    text: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("kind IN ('start', 'end')"),
        CheckConstraint("status IN ('pending', 'sent', 'failed', 'expired')"),
        Index("uq_analysis_notification_kind", "opportunity_id", "kind", unique=True),
        Index("ix_analysis_notifications_status_created", "status", "created_at"),
    )
