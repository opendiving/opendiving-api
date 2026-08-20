"""index authentication_request.expires_at for the expiry sweep

`core.worker.functions.purge_expired_authentication_requests` runs hourly and selects on
`expires_at < :cutoff`. Same argument the equivalent index on `token_blacklist.expires_at`
was added under - a sweep that scans the whole table gets slower exactly as the table gets
big enough to need sweeping - and this table starts out worse: nothing has ever deleted a
row from it, so the first run on an existing instance meets everything ever written.

Revision ID: b24933e17c19
Revises: f7710514ddde
Create Date: 2026-08-21 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b24933e17c19"
down_revision: str | None = "f7710514ddde"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        op.f("ix_authentication_request_expires_at"), "authentication_request", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_authentication_request_expires_at"), table_name="authentication_request")
