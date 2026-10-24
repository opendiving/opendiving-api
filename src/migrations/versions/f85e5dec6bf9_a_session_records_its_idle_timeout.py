"""a session records its idle timeout

Revision ID: f85e5dec6bf9
Revises: 25ba09ac7bf5
Create Date: 2026-10-24 12:00:00.000000

One column, not null with a server default, so every existing session reads `standard` - the
only window there was - and keeps sliding by `REFRESH_TOKEN_EXPIRE_DAYS` exactly as before.
`downgrade()` drops it, and an `extended` session then slides by the standard window from its
next refresh.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f85e5dec6bf9"
down_revision: str | None = "25ba09ac7bf5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_session",
        sa.Column("idle_timeout", sa.String(length=16), server_default="standard", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("user_session", "idle_timeout")
