"""external requests are counted per day

Revision ID: 90c6f077a7d4
Revises: 800c913889b4
Create Date: 2026-11-17 07:54:19.001507

A new table, empty: nothing was counted before it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "90c6f077a7d4"
down_revision: str | None = "800c913889b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "daily_external_request",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("duration_ms_total", sa.BigInteger(), nullable=False),
        sa.Column("duration_ms_max", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("day", "provider", "kind", "outcome"),
    )


def downgrade() -> None:
    op.drop_table("daily_external_request")
