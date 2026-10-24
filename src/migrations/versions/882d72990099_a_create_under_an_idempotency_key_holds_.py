"""a create under an Idempotency-Key holds the record it made

Revision ID: 882d72990099
Revises: 1f96cf18d60e
Create Date: 2026-10-24 15:32:03.623767

A new table, and nothing else: a request without the header writes no row, so every existing
record and route reads as before. `downgrade()` drops it, and a retry the dropped hold would
have answered creates again.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "882d72990099"
down_revision: str | None = "1f96cf18d60e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_hold",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("route", sa.String(length=255), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("status_code", sa.SmallInteger(), nullable=False),
        sa.Column("record_uuid", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_idempotency_hold_created_at"), "idempotency_hold", ["created_at"], unique=False)
    op.create_index(
        "ux_idempotency_hold_user_id_route_key", "idempotency_hold", ["user_id", "route", "key"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ux_idempotency_hold_user_id_route_key", table_name="idempotency_hold")
    op.drop_index(op.f("ix_idempotency_hold_created_at"), table_name="idempotency_hold")
    op.drop_table("idempotency_hold")
