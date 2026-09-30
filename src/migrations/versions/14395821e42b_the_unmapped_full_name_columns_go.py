"""the unmapped full_name columns go

Revision ID: 14395821e42b
Revises: 0f941e1c4130
Create Date: 2026-10-01 02:28:06.024721

`dive_site.location_full_name` and `trip_part.full_name` go, leaving a place the one name it
was saved with. The build this deploy replaces maps neither column, so running beside it fails
nothing: *A column the serving build maps is dropped a deploy after it is unmapped* in
`DECISIONS.md`.

The stored values are dropped unread: every saved place keeps its name, and one that should
name its region is re-picked. `downgrade()` re-adds both columns nullable and empty - nothing
else on the row holds the longer form, so there is nothing to copy back.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "14395821e42b"
down_revision: str | None = "0f941e1c4130"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("dive_site", "location_full_name")
    op.drop_column("trip_part", "full_name")


def downgrade() -> None:
    op.add_column("trip_part", sa.Column("full_name", sa.String(length=512), nullable=True))
    op.add_column("dive_site", sa.Column("location_full_name", sa.String(length=512), nullable=True))
