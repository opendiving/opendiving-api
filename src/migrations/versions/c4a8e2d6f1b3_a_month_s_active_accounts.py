"""A month's active accounts

Revision ID: c4a8e2d6f1b3
Revises: b3f1c7a9d2e4
Create Date: 2026-11-14 12:00:00.000000

`monthly_active_account` is created empty, and nothing is backfilled: the session sweep
fills it from the sessions it next reads, so the month it ships in is counted from the
sessions still stored and every month before it holds no count.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4a8e2d6f1b3"
down_revision: str | None = "b3f1c7a9d2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "monthly_active_account",
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("month", "user_id"),
    )
    op.create_index(op.f("ix_monthly_active_account_user_id"), "monthly_active_account", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_monthly_active_account_user_id"), table_name="monthly_active_account")
    op.drop_table("monthly_active_account")
