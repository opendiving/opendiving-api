"""a species photo records what a human decided, and how wide its bytes are

Revision ID: ddee65013c16
Revises: 580d29f6c848
Create Date: 2026-10-07 18:00:00.000000

Three nullable columns on `species`. No data path: every existing row reads as "the rule
decides, dimensions unknown", which is true of it, and the backfill's `--recheck-size` pass
measures the stored photos.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ddee65013c16"
down_revision: str | None = "580d29f6c848"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("species", sa.Column("photo_curation", sa.String(length=16), nullable=True))
    op.add_column("species", sa.Column("photo_width", sa.Integer(), nullable=True))
    op.add_column("species", sa.Column("photo_height", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("species", "photo_height")
    op.drop_column("species", "photo_width")
    op.drop_column("species", "photo_curation")
