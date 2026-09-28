"""Añade registro aislado de órdenes manuales.

Revision ID: b913fa202600
Revises: a816c741c120
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b913fa202600"
down_revision: str | None = "a816c741c120"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "manual_orders",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_index("ix_manual_orders_status", "manual_orders", ["status"])


def downgrade() -> None:
    op.drop_index("ix_manual_orders_status", "manual_orders")
    op.drop_table("manual_orders")
