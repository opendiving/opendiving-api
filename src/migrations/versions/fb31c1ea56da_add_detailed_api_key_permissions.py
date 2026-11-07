"""add detailed API key permissions

Revision ID: fb31c1ea56da
Revises: f1f451dacfb7
Create Date: 2026-11-01 21:41:04.408751

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fb31c1ea56da"
down_revision: str | None = "f1f451dacfb7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
