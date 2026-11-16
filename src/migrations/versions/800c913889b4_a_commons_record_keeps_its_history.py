"""a commons record keeps its history, and a proposal whether its proposer is named

Revision ID: 800c913889b4
Revises: b8e2d5a41c07
Create Date: 2026-11-16 16:34:10.675927

The table starts empty: no entry is made for a proposal accepted before it. `show_proposer` is
false on every stored proposal, which is what each was sent with.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "800c913889b4"
down_revision: str | None = "b8e2d5a41c07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dive_site_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dive_site_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("proposal_id", sa.Integer(), nullable=True),
        sa.Column("reviewer_id", sa.Integer(), nullable=True),
        sa.Column("corrected", sa.Boolean(), nullable=True),
        sa.Column("uuid", sa.UUID(), nullable=False),
        sa.CheckConstraint("corrected IS NOT NULL OR kind = 'changed'", name="ck_dive_site_history_correction_kind"),
        sa.CheckConstraint("kind IN ('added', 'changed')", name="ck_dive_site_history_kind"),
        sa.CheckConstraint("corrected IS NOT NULL OR proposal_id IS NULL", name="ck_dive_site_history_correction"),
        sa.ForeignKeyConstraint(["dive_site_id"], ["dive_site.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["proposal_id"], ["dive_site_proposal.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewer_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_dive_site_history_dive_site_id_created_at",
        "dive_site_history",
        ["dive_site_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(op.f("ix_dive_site_history_proposal_id"), "dive_site_history", ["proposal_id"], unique=False)
    op.create_index(op.f("ix_dive_site_history_reviewer_id"), "dive_site_history", ["reviewer_id"], unique=False)
    op.create_index(op.f("ix_dive_site_history_uuid"), "dive_site_history", ["uuid"], unique=True)
    op.add_column(
        "dive_site_proposal", sa.Column("show_proposer", sa.Boolean(), server_default="false", nullable=False)
    )


def downgrade() -> None:
    op.drop_column("dive_site_proposal", "show_proposer")
    op.drop_index(op.f("ix_dive_site_history_uuid"), table_name="dive_site_history")
    op.drop_index(op.f("ix_dive_site_history_reviewer_id"), table_name="dive_site_history")
    op.drop_index(op.f("ix_dive_site_history_proposal_id"), table_name="dive_site_history")
    op.drop_index("ix_dive_site_history_dive_site_id_created_at", table_name="dive_site_history")
    op.drop_table("dive_site_history")
