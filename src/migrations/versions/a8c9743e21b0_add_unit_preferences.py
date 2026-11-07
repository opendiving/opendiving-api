"""Add independent unit preferences, preserving every account's existing preset."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8c9743e21b0"
down_revision: str | None = "417c0a090fd8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user", sa.Column("unit_preferences", sa.JSON(), server_default="{}", nullable=False))


def downgrade() -> None:
    op.drop_column("user", "unit_preferences")
