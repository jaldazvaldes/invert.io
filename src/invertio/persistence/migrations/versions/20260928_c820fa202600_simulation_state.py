"""Cartera virtual persistente para el análisis, aislada de los otros historiales.

Revision ID: c820fa202600
Revises: b913fa202600
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c820fa202600"
down_revision: str | None = "b913fa202600"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "simulation_state",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("simulation_state")
