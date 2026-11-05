"""a site is on a diver's list through a saved row that holds their notes

Revision ID: 63b752e6b762
Revises: 3b5134d444b3
Create Date: 2026-11-05 12:00:00.000000

Every site gains one `saved_site` row for its owner, carrying the site's notes and
`created_at`. `dive_site.notes` stays on the table: the previous build reads and writes it
until this build is serving, and it leaves in a later revision, which reconciles what that build
wrote alone. The server default is what lets this build insert a site without naming it.

`downgrade()` copies each owner's notes back, and any other row is gone with the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "63b752e6b762"
down_revision: str | None = "3b5134d444b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "saved_site",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("dive_site_id", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["dive_site_id"], ["dive_site.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "dive_site_id", name="ux_saved_site_user_id_dive_site_id"),
    )
    op.create_index(op.f("ix_saved_site_dive_site_id"), "saved_site", ["dive_site_id"], unique=False)
    op.alter_column("dive_site", "notes", existing_type=sa.Text(), existing_nullable=False, server_default="")
    op.execute(
        "INSERT INTO saved_site (user_id, dive_site_id, notes, created_at) "
        "SELECT user_id, id, notes, created_at FROM dive_site"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE dive_site SET notes = saved_site.notes FROM saved_site "
        "WHERE saved_site.dive_site_id = dive_site.id AND saved_site.user_id = dive_site.user_id"
    )
    op.alter_column("dive_site", "notes", existing_type=sa.Text(), existing_nullable=False, server_default=None)
    op.drop_index(op.f("ix_saved_site_dive_site_id"), table_name="saved_site")
    op.drop_table("saved_site")
