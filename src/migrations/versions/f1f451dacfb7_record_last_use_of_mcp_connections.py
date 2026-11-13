"""record last use of MCP connections

Revision ID: f1f451dacfb7
Revises: d2efc95305b6
Create Date: 2026-11-01 21:16:34.380642

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1f451dacfb7"
down_revision: str | None = "d2efc95305b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("mcp_grant", sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        sa.text(
            """
            UPDATE mcp_grant SET last_used_at = activity.last_used_at
            FROM (
                SELECT grant_id, MAX(created_at) AS last_used_at
                FROM mcp_audit GROUP BY grant_id
            ) AS activity
            WHERE mcp_grant.id = activity.grant_id
            """
        )
    )


def downgrade() -> None:
    op.drop_column("mcp_grant", "last_used_at")
