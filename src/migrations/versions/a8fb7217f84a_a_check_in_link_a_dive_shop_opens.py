"""a check-in link a dive shop opens

Revision ID: a8fb7217f84a
Revises: 5726a8af2fb6
Create Date: 2026-09-26 08:17:57.367957

A new table and nothing else: the check-in links a diver mints, each a hashed token with a
day to live and the diving figures the page showed. Additive, with nothing to backfill.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8fb7217f84a"
down_revision: str | None = "5726a8af2fb6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "checkin_link",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_dives", sa.Integer(), nullable=True),
        sa.Column("max_depth", sa.Float(), nullable=True),
        sa.Column("last_dive_on", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_checkin_link_expires_at"), "checkin_link", ["expires_at"], unique=False)
    op.create_index(op.f("ix_checkin_link_token_hash"), "checkin_link", ["token_hash"], unique=True)
    op.create_index(op.f("ix_checkin_link_user_id"), "checkin_link", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_checkin_link_user_id"), table_name="checkin_link")
    op.drop_index(op.f("ix_checkin_link_token_hash"), table_name="checkin_link")
    op.drop_index(op.f("ix_checkin_link_expires_at"), table_name="checkin_link")
    op.drop_table("checkin_link")
