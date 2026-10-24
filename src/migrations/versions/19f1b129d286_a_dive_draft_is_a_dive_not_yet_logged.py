"""a dive draft is a dive not yet logged

Revision ID: 19f1b129d286
Revises: 882d72990099
Create Date: 2026-10-24 19:10:58.090930

A new table, and nothing else: no existing row is read or written. `downgrade()` drops it, and
every draft with it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "19f1b129d286"
down_revision: str | None = "882d72990099"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dive_draft",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("values", sa.JSON(), nullable=False),
        sa.Column("gear_set_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("uuid", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["gear_set_id"], ["gear_set.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_dive_draft_gear_set_id"), "dive_draft", ["gear_set_id"], unique=False)
    op.create_index("ix_dive_draft_user_id_updated_at", "dive_draft", ["user_id", "updated_at"], unique=False)
    op.create_index(op.f("ix_dive_draft_uuid"), "dive_draft", ["uuid"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_dive_draft_uuid"), table_name="dive_draft")
    op.drop_index("ix_dive_draft_user_id_updated_at", table_name="dive_draft")
    op.drop_index(op.f("ix_dive_draft_gear_set_id"), table_name="dive_draft")
    op.drop_table("dive_draft")
