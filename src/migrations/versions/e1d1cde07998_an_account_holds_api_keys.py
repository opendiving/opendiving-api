"""an account holds API keys

Revision ID: e1d1cde07998
Revises: 19f1b129d286
Create Date: 2026-10-28 15:45:04.115233

A new table, and nothing else: no existing row is read or written. `downgrade()` drops it, and
every API key with it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1d1cde07998"
down_revision: str | None = "19f1b129d286"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_key",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("prefix", sa.String(length=12), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("uuid", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_api_key_token_hash"), "api_key", ["token_hash"], unique=True)
    op.create_index(op.f("ix_api_key_user_id"), "api_key", ["user_id"], unique=False)
    op.create_index(op.f("ix_api_key_uuid"), "api_key", ["uuid"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_api_key_uuid"), table_name="api_key")
    op.drop_index(op.f("ix_api_key_user_id"), table_name="api_key")
    op.drop_index(op.f("ix_api_key_token_hash"), table_name="api_key")
    op.drop_table("api_key")
