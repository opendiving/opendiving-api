"""A species photo proposal and what the operator made of it

Revision ID: e316b5f7b995
Revises: 65940d01a99c
Create Date: 2026-11-13 22:37:41.258311

`species_photo_proposal` holds one account's word that a Commons file would be a better photo for a
species, and how it ended. It adds a table and touches no row.

`downgrade()` drops it. A photo pinned from an accepted proposal stays on its species.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e316b5f7b995"
down_revision: str | None = "65940d01a99c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "species_photo_proposal",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("species_id", sa.Integer(), nullable=False),
        sa.Column("file", sa.String(length=255), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("uuid", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('waiting', 'accepted', 'declined', 'withdrawn', 'closed')",
            name="ck_species_photo_proposal_status",
        ),
        sa.ForeignKeyConstraint(["species_id"], ["species.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_species_photo_proposal_species_id"), "species_photo_proposal", ["species_id"], unique=False
    )
    op.create_index(op.f("ix_species_photo_proposal_user_id"), "species_photo_proposal", ["user_id"], unique=False)
    op.create_index(op.f("ix_species_photo_proposal_uuid"), "species_photo_proposal", ["uuid"], unique=True)
    op.create_index(
        "ux_species_photo_proposal_one_waiting",
        "species_photo_proposal",
        ["user_id", "species_id"],
        unique=True,
        postgresql_where=sa.text("status = 'waiting'"),
    )


def downgrade() -> None:
    op.drop_table("species_photo_proposal")
