"""an invitation no longer marks a connect-link sign-up

`invitation.from_invite_link` is dropped. It told the member's list of their invitations which
rows a sign-up through their link wrote, so that list could show a name rather than an address
the member never typed; that list is gone. The rows stay, each an accepted invitation the quota
counts.

`downgrade()` adds the column back false on every row: which accepted rows a link wrote is not
recoverable from what remains.

Revision ID: b3c41e7f9a20
Revises: 36f9496a7b33
Create Date: 2026-11-23 23:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3c41e7f9a20"
down_revision: str | None = "36f9496a7b33"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("invitation", "from_invite_link")


def downgrade() -> None:
    op.add_column("invitation", sa.Column("from_invite_link", sa.Boolean(), server_default="false", nullable=False))
