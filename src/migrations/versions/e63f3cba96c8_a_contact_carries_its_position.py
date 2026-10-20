"""a contact carries its position

Revision ID: e63f3cba96c8
Revises: 233676c05c83
Create Date: 2026-10-20 22:51:16.088694

Two nullable floats, as a dive site's pin is stored. Adding a nullable column with no default
is a catalog change in Postgres: no table rewrite, and every existing contact reads as having
no position.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e63f3cba96c8"
down_revision: str | None = "233676c05c83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("contact", sa.Column("latitude", sa.Float(), nullable=True))
    op.add_column("contact", sa.Column("longitude", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("contact", "longitude")
    op.drop_column("contact", "latitude")
