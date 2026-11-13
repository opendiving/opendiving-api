"""An account keeps its dive computers in an order

Revision ID: b3f1c7a9d2e4
Revises: e316b5f7b995
Create Date: 2026-11-13 12:00:00.000000

One JSON column on `user`, the diver's order of their dive computers by serial. Every
existing account gets the empty list from the server default, and no row is rewritten: an
account that has placed no computer orders them by when it first received a recording from
each, which is what the recordings already say.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3f1c7a9d2e4"
down_revision: str | None = "e316b5f7b995"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user", sa.Column("dive_computer_order", sa.JSON(), server_default="[]", nullable=False))


def downgrade() -> None:
    op.drop_column("user", "dive_computer_order")
