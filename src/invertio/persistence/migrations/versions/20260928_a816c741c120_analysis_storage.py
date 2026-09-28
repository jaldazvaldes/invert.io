"""Add isolated analysis observations, opportunities, state and notification outbox.

Revision ID: a816c741c120
Revises: 03d7ee18ac9b
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from invertio.persistence.types import UTCDateTime

revision: str = "a816c741c120"
down_revision: str | None = "03d7ee18ac9b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_observations",
        sa.Column("market", sa.String(96), primary_key=True),
        sa.Column("bar_ts", UTCDateTime(), primary_key=True),
        sa.Column("rule_version", sa.String(64), primary_key=True),
        sa.Column("observed_at", UTCDateTime(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_index("ix_analysis_observations_observed", "analysis_observations", ["observed_at"])
    op.create_table(
        "analysis_opportunities",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("market", sa.String(96), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.CheckConstraint("status IN ('open', 'closed', 'interrupted')"),
    )
    op.create_index("ix_analysis_opportunities_created", "analysis_opportunities", ["created_at"])
    op.create_index(
        "uq_analysis_open_market",
        "analysis_opportunities",
        ["market"],
        unique=True,
        sqlite_where=sa.text("status = 'open'"),
    )
    op.create_table(
        "analysis_state",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_table(
        "analysis_notifications",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column(
            "opportunity_id",
            sa.String(64),
            sa.ForeignKey("analysis_opportunities.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_at", UTCDateTime(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.CheckConstraint("kind IN ('start', 'end')"),
        sa.CheckConstraint("status IN ('pending', 'sent', 'failed', 'expired')"),
    )
    op.create_index(
        "uq_analysis_notification_kind",
        "analysis_notifications",
        ["opportunity_id", "kind"],
        unique=True,
    )
    op.create_index(
        "ix_analysis_notifications_status_created",
        "analysis_notifications",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("analysis_notifications")
    op.drop_table("analysis_state")
    op.drop_table("analysis_opportunities")
    op.drop_table("analysis_observations")
