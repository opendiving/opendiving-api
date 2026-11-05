"""Propose a site or a change for OpenDiving Commons, and review it

Revision ID: b4d27e6f1c03
Revises: 8c1e4f2a9d37
Create Date: 2026-11-05 22:10:00.000000

`dive_site_proposal` holds what a diver asked for and what became of it, and
`dive_site.commons_reviewed_at` marks a row a review wrote, which the sync then leaves alone.

`downgrade()` drops both. An accepted record stays a Commons row, unmarked, so a copy's sync can
overwrite it again.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4d27e6f1c03"
down_revision: str | None = "8c1e4f2a9d37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("dive_site", sa.Column("commons_reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "dive_site_proposal",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("dive_site_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("members", sa.JSON(), nullable=False),
        sa.Column("attestation", sa.Text(), nullable=False),
        sa.Column("attested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("uuid", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('new_site', 'change')", name="ck_dive_site_proposal_kind"),
        sa.CheckConstraint(
            "status IN ('waiting', 'accepted', 'declined', 'withdrawn')", name="ck_dive_site_proposal_status"
        ),
        sa.CheckConstraint(
            "status <> 'declined' OR reason IS NOT NULL", name="ck_dive_site_proposal_declined_has_reason"
        ),
        sa.ForeignKeyConstraint(["dive_site_id"], ["dive_site.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_dive_site_proposal_dive_site_id"), "dive_site_proposal", ["dive_site_id"], unique=False)
    op.create_index(op.f("ix_dive_site_proposal_user_id"), "dive_site_proposal", ["user_id"], unique=False)
    op.create_index(op.f("ix_dive_site_proposal_uuid"), "dive_site_proposal", ["uuid"], unique=True)
    op.create_index(
        "ux_dive_site_proposal_one_waiting",
        "dive_site_proposal",
        ["user_id", "dive_site_id"],
        unique=True,
        postgresql_where=sa.text("status = 'waiting'"),
    )


def downgrade() -> None:
    op.drop_table("dive_site_proposal")
    op.drop_column("dive_site", "commons_reviewed_at")
