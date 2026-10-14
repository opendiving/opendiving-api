"""a member's invite link

Revision ID: ea26677dea89
Revises: 7025991d9229
Create Date: 2026-10-14 08:42:50.000000

A new table, `invite_link`, one row per member holding the token their link carries; and
`invitation.from_invite_link`, which marks the accepted row a sign-up through that link writes.
Additive: the column's server default answers false for every existing row and for the previous
build's inserts in a deploy overlap, and there is nothing to backfill.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ea26677dea89"
down_revision: str | None = "7025991d9229"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "invite_link",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_invite_link_token"), "invite_link", ["token"], unique=True)
    op.create_index(op.f("ix_invite_link_user_id"), "invite_link", ["user_id"], unique=True)
    op.add_column("invitation", sa.Column("from_invite_link", sa.Boolean(), server_default="false", nullable=False))


def downgrade() -> None:
    op.drop_column("invitation", "from_invite_link")
    op.drop_index(op.f("ix_invite_link_user_id"), table_name="invite_link")
    op.drop_index(op.f("ix_invite_link_token"), table_name="invite_link")
    op.drop_table("invite_link")
