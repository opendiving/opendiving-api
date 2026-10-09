"""the account remembers its dive form preset

One nullable column on `user` with its index and an `ON DELETE SET NULL` key into
`dive_form_preset.uuid`. No backfill: null is "nothing picked", which every existing account
is until its diver next applies a preset.

Revision ID: 1538257d95db
Revises: ddee65013c16
Create Date: 2026-10-09 10:58:44.832536

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1538257d95db"
down_revision: str | None = "ddee65013c16"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user", sa.Column("dive_form_preset_uuid", sa.UUID(), nullable=True))
    op.create_index(op.f("ix_user_dive_form_preset_uuid"), "user", ["dive_form_preset_uuid"], unique=False)
    op.create_foreign_key(
        "user_dive_form_preset_uuid_fkey",
        "user",
        "dive_form_preset",
        ["dive_form_preset_uuid"],
        ["uuid"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("user_dive_form_preset_uuid_fkey", "user", type_="foreignkey")
    op.drop_index(op.f("ix_user_dive_form_preset_uuid"), table_name="user")
    op.drop_column("user", "dive_form_preset_uuid")
