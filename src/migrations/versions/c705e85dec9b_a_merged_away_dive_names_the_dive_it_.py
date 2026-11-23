"""a merged-away dive names the dive it joined

One nullable column on `dive` with its index and an `ON DELETE SET NULL` key into `dive.id`. A
merge writes it on the dive it soft-deletes. No backfill: a merge made before this revision is
not marked, and an import finds one by what the merged dive holds.

Revision ID: c705e85dec9b
Revises: c3f81a6d9e20
Create Date: 2026-11-23 13:43:27.093888

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c705e85dec9b"
down_revision: str | None = "c3f81a6d9e20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("dive", sa.Column("merged_into_id", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_dive_merged_into_id"), "dive", ["merged_into_id"], unique=False)
    op.create_foreign_key("dive_merged_into_id_fkey", "dive", "dive", ["merged_into_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    op.drop_constraint("dive_merged_into_id_fkey", "dive", type_="foreignkey")
    op.drop_index(op.f("ix_dive_merged_into_id"), table_name="dive")
    op.drop_column("dive", "merged_into_id")
