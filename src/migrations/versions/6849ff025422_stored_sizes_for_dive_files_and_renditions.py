"""stored sizes for dive files and renditions

Revision ID: 6849ff025422
Revises: c47b308253a3
Create Date: 2026-09-28 10:00:00.000000

The storage limit counts what an object occupies, and two tables gain the column that says so.

`dive_file.stored_byte_size` is filled from `byte_size`, in SQL: no object written before this
revision is a zstd frame, so every existing one occupies exactly its upload's length. `NOT NULL`
once filled. Between this revision running and traffic moving to the new build, the outgoing
build's dive-file insert names no size and fails - an attach answers its 409 and asks for a
retry, an import rolls back.

`user_picture.rendition_byte_size` is added empty and stays nullable. A rendition's length is
its object's, and a revision is frozen history that reads no live storage code, so it cannot
ask the store; the lifespan measures every null one through the live store instead
(`core/setup.py`, `measure_unsized_renditions`). A rendition the outgoing build writes in the
overlap stays null until the next boot measures it.

`downgrade()` drops both columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6849ff025422"
down_revision: str | None = "c47b308253a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("dive_file", sa.Column("stored_byte_size", sa.Integer(), nullable=True))
    op.execute("UPDATE dive_file SET stored_byte_size = byte_size")
    op.alter_column("dive_file", "stored_byte_size", existing_type=sa.Integer(), nullable=False)
    op.add_column("user_picture", sa.Column("rendition_byte_size", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_picture", "rendition_byte_size")
    op.drop_column("dive_file", "stored_byte_size")
