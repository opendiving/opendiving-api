"""a profile records the reader version that read it

Revision ID: a84d94bb0272
Revises: e02a39ada562
Create Date: 2026-09-28 20:16:18.460577

`dive_profile.reader_version` is added empty and no row is stamped: NULL is what "not read by
this instance's reader" means, which is true of every profile stored so far, so each one read
from a stored file becomes a candidate for `backfill_dive_profiles` - the data path, which an
operator runs once this build is live. Until then every profile is served as it is.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a84d94bb0272"
down_revision: str | None = "e02a39ada562"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("dive_profile", sa.Column("reader_version", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("dive_profile", "reader_version")
