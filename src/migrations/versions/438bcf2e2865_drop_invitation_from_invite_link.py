"""drop invitation from_invite_link

Revision ID: 438bcf2e2865
Revises: 36f9496a7b33
Create Date: 2026-11-24 01:42:53.590307

The column left the mapper a deploy earlier, and nothing has read it since; that build writes it
until this one replaces it. `downgrade()` restores it `false` on every row, so what a connect-link
sign-up wrote into it is not recovered.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "438bcf2e2865"
down_revision: str | None = "36f9496a7b33"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("invitation", "from_invite_link")


def downgrade() -> None:
    op.add_column(
        "invitation",
        sa.Column(
            "from_invite_link", sa.BOOLEAN(), server_default=sa.text("false"), autoincrement=False, nullable=False
        ),
    )
