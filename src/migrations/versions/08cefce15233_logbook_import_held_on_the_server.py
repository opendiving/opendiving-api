"""An import the server holds, and the files it holds

Revision ID: 08cefce15233
Revises: d3f8a61c27e4
Create Date: 2026-11-07 10:24:51.395112

`logbook_import` holds an import from its upload to the end of its history, one `pending` per
account, and `logbook_import_file` the readable files of a pending one, by their storage keys.

`downgrade()` drops both. The blobs they name become orphans for the sweeper.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "08cefce15233"
down_revision: str | None = "d3f8a61c27e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "logbook_import",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("review_shape", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review", sa.JSON(), nullable=False),
        sa.Column("notes", sa.JSON(), nullable=False),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.Column("remaps", sa.JSON(), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("uuid", sa.UUID(), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'applied')", name="ck_logbook_import_status"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_logbook_import_expires_at", "logbook_import", ["expires_at"], unique=False)
    op.create_index(
        "ix_logbook_import_user_id_status_applied_at",
        "logbook_import",
        ["user_id", "status", "applied_at"],
        unique=False,
    )
    op.create_index(op.f("ix_logbook_import_uuid"), "logbook_import", ["uuid"], unique=True)
    op.create_index(
        "ux_logbook_import_one_pending",
        "logbook_import",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "logbook_import_file",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("logbook_import_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=255), nullable=False),
        sa.Column("part", sa.Integer(), nullable=True),
        sa.CheckConstraint("role IN ('part', 'portrait')", name="ck_logbook_import_file_role"),
        sa.ForeignKeyConstraint(["logbook_import_id"], ["logbook_import.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index(
        op.f("ix_logbook_import_file_logbook_import_id"), "logbook_import_file", ["logbook_import_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_logbook_import_file_logbook_import_id"), table_name="logbook_import_file")
    op.drop_table("logbook_import_file")
    op.drop_index(
        "ux_logbook_import_one_pending", table_name="logbook_import", postgresql_where=sa.text("status = 'pending'")
    )
    op.drop_index(op.f("ix_logbook_import_uuid"), table_name="logbook_import")
    op.drop_index("ix_logbook_import_user_id_status_applied_at", table_name="logbook_import")
    op.drop_index("ix_logbook_import_expires_at", table_name="logbook_import")
    op.drop_table("logbook_import")
