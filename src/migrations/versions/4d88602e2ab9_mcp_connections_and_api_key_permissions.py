"""MCP connections, file handles and detailed API-key permissions

Revision ID: 4d88602e2ab9
Revises: 65940d01a99c
Create Date: 2026-11-13 12:00:00.000000

Delegated MCP authorization (clients, connections with their last use, codes, tokens, tool-call
audit and expiring file handles) and the `scoped` API-key mode with its permission list. Nothing
is backfilled: every table is new, and existing keys keep `scope` `read` or `write` with no
`scopes`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "4d88602e2ab9"
down_revision: str | None = "65940d01a99c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_client",
        sa.Column("client_id", sa.String(length=512), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("client_id"),
    )
    op.create_table(
        "mcp_grant",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.String(length=512), nullable=False),
        sa.Column("scopes", sa.String(length=4096), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("uuid", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["mcp_client.client_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_mcp_grant_client_id"), "mcp_grant", ["client_id"], unique=False)
    op.create_index(op.f("ix_mcp_grant_user_id"), "mcp_grant", ["user_id"], unique=False)
    op.create_index(op.f("ix_mcp_grant_uuid"), "mcp_grant", ["uuid"], unique=True)
    op.create_table(
        "mcp_audit",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("grant_id", sa.Integer(), nullable=False),
        sa.Column("tool", sa.String(length=128), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("uuid", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["grant_id"], ["mcp_grant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_mcp_audit_grant_id"), "mcp_audit", ["grant_id"], unique=False)
    op.create_index(op.f("ix_mcp_audit_uuid"), "mcp_audit", ["uuid"], unique=True)
    op.create_table(
        "mcp_authorization",
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(length=512), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("grant_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["client_id"], ["mcp_client.client_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["grant_id"], ["mcp_grant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("secret_hash"),
    )
    op.create_index(op.f("ix_mcp_authorization_grant_id"), "mcp_authorization", ["grant_id"], unique=False)
    op.create_index(op.f("ix_mcp_authorization_client_id"), "mcp_authorization", ["client_id"], unique=False)
    op.create_index(op.f("ix_mcp_authorization_expires_at"), "mcp_authorization", ["expires_at"], unique=False)
    op.create_table(
        "mcp_file",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("grant_id", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("uuid", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["grant_id"], ["mcp_grant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_mcp_file_expires_at"), "mcp_file", ["expires_at"], unique=False)
    op.create_index(op.f("ix_mcp_file_grant_id"), "mcp_file", ["grant_id"], unique=False)
    op.create_index(op.f("ix_mcp_file_user_id"), "mcp_file", ["user_id"], unique=False)
    op.create_index(op.f("ix_mcp_file_uuid"), "mcp_file", ["uuid"], unique=True)
    op.create_table(
        "mcp_token",
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("grant_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("scopes", sa.String(length=4096), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("spent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["grant_id"], ["mcp_grant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("secret_hash"),
    )
    op.create_index(op.f("ix_mcp_token_expires_at"), "mcp_token", ["expires_at"], unique=False)
    op.create_index(op.f("ix_mcp_token_grant_id"), "mcp_token", ["grant_id"], unique=False)

    op.add_column("api_key", sa.Column("scopes", sa.ARRAY(sa.String(length=32)), nullable=True))
    op.create_check_constraint(
        "api_key_permission_mode",
        "api_key",
        "(scope IN ('read', 'write') AND scopes IS NULL) OR "
        "(scope = 'scoped' AND scopes IS NOT NULL AND cardinality(scopes) > 0)",
    )


def downgrade() -> None:
    # Older APIs interpret an unknown mode as broad read access, so rollback must revoke scoped keys.
    op.execute("DELETE FROM api_key WHERE scope = 'scoped'")
    op.drop_constraint("api_key_permission_mode", "api_key", type_="check")
    op.drop_column("api_key", "scopes")
    op.drop_index(op.f("ix_mcp_token_grant_id"), table_name="mcp_token")
    op.drop_index(op.f("ix_mcp_token_expires_at"), table_name="mcp_token")
    op.drop_table("mcp_token")
    op.drop_index(op.f("ix_mcp_file_uuid"), table_name="mcp_file")
    op.drop_index(op.f("ix_mcp_file_user_id"), table_name="mcp_file")
    op.drop_index(op.f("ix_mcp_file_grant_id"), table_name="mcp_file")
    op.drop_index(op.f("ix_mcp_file_expires_at"), table_name="mcp_file")
    op.drop_table("mcp_file")
    op.drop_index(op.f("ix_mcp_authorization_expires_at"), table_name="mcp_authorization")
    op.drop_index(op.f("ix_mcp_authorization_client_id"), table_name="mcp_authorization")
    op.drop_index(op.f("ix_mcp_authorization_grant_id"), table_name="mcp_authorization")
    op.drop_table("mcp_authorization")
    op.drop_index(op.f("ix_mcp_audit_uuid"), table_name="mcp_audit")
    op.drop_index(op.f("ix_mcp_audit_grant_id"), table_name="mcp_audit")
    op.drop_table("mcp_audit")
    op.drop_index(op.f("ix_mcp_grant_uuid"), table_name="mcp_grant")
    op.drop_index(op.f("ix_mcp_grant_user_id"), table_name="mcp_grant")
    op.drop_index(op.f("ix_mcp_grant_client_id"), table_name="mcp_grant")
    op.drop_table("mcp_grant")
    op.drop_table("mcp_client")
