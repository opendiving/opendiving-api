"""A replaced site leaves an alias

Revision ID: 04c319ff9731
Revises: 08cefce15233
Create Date: 2026-11-07 12:47:06.984740

For one account, the uuid of a site it replaced and the Commons record that uuid now means. A new
table, empty: nothing holds the uuid of a site already replaced, so there is nothing to backfill.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "04c319ff9731"
down_revision: str | None = "08cefce15233"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dive_site_alias",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("alias_uuid", sa.UUID(), nullable=False),
        sa.Column("dive_site_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["dive_site_id"], ["dive_site.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "alias_uuid", name="ux_dive_site_alias_user_id_alias_uuid"),
    )
    op.create_index(op.f("ix_dive_site_alias_dive_site_id"), "dive_site_alias", ["dive_site_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_dive_site_alias_dive_site_id"), table_name="dive_site_alias")
    op.drop_table("dive_site_alias")
