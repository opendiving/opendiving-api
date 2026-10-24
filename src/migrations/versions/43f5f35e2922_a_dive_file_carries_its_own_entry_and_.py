"""a dive file carries its own entry and exit fixes

Revision ID: 43f5f35e2922
Revises: f85e5dec6bf9
Create Date: 2026-10-24 16:27:23.039716

Adds the columns empty, with `fixes_read` false on every existing row: a revision has neither the
blob store nor the reader, so the worker's `read_unread_file_fixes` reads the stored bytes and
re-derives each dive's fixes from them on its first run. `downgrade()` drops the columns, and the
dive's own fixes stay as the last derivation left them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "43f5f35e2922"
down_revision: str | None = "f85e5dec6bf9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("dive_file", sa.Column("entry_latitude", sa.Float(), nullable=True))
    op.add_column("dive_file", sa.Column("entry_longitude", sa.Float(), nullable=True))
    op.add_column("dive_file", sa.Column("exit_latitude", sa.Float(), nullable=True))
    op.add_column("dive_file", sa.Column("exit_longitude", sa.Float(), nullable=True))
    op.add_column("dive_file", sa.Column("fixes_read", sa.Boolean(), server_default="false", nullable=False))
    op.create_check_constraint(
        "ck_dive_file_entry_position_pair", "dive_file", "(entry_latitude IS NULL) = (entry_longitude IS NULL)"
    )
    op.create_check_constraint(
        "ck_dive_file_exit_position_pair", "dive_file", "(exit_latitude IS NULL) = (exit_longitude IS NULL)"
    )


def downgrade() -> None:
    op.drop_constraint("ck_dive_file_exit_position_pair", "dive_file", type_="check")
    op.drop_constraint("ck_dive_file_entry_position_pair", "dive_file", type_="check")
    op.drop_column("dive_file", "fixes_read")
    op.drop_column("dive_file", "exit_longitude")
    op.drop_column("dive_file", "exit_latitude")
    op.drop_column("dive_file", "entry_longitude")
    op.drop_column("dive_file", "entry_latitude")
