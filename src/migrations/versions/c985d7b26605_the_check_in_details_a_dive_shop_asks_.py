"""the check-in details a dive shop asks for

Revision ID: c985d7b26605
Revises: 9f55a06b2f49
Create Date: 2026-09-17 13:41:56.546380

Eight columns on `user`: date of birth, phone, the emergency contact's name, phone and
relationship, and the insurance provider, policy number and expiry.

Every one is nullable, so this is purely additive - no existing row changes, nothing needs
backfilling, and no column needs a `server_default` to give the rows already in the table a
value. An account that never fills any of them in reads exactly as it did before.

`downgrade()` drops them, and with them whatever divers have entered. There is nowhere else
that data lives.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c985d7b26605"
down_revision: str | None = "9f55a06b2f49"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user", sa.Column("date_of_birth", sa.Date(), nullable=True))
    op.add_column("user", sa.Column("phone", sa.String(length=32), nullable=True))
    op.add_column("user", sa.Column("emergency_contact_name", sa.String(length=100), nullable=True))
    op.add_column("user", sa.Column("emergency_contact_phone", sa.String(length=32), nullable=True))
    op.add_column("user", sa.Column("emergency_contact_relationship", sa.String(length=50), nullable=True))
    op.add_column("user", sa.Column("insurance_provider", sa.String(length=100), nullable=True))
    op.add_column("user", sa.Column("insurance_policy_number", sa.String(length=64), nullable=True))
    op.add_column("user", sa.Column("insurance_expires_on", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("user", "insurance_expires_on")
    op.drop_column("user", "insurance_policy_number")
    op.drop_column("user", "insurance_provider")
    op.drop_column("user", "emergency_contact_relationship")
    op.drop_column("user", "emergency_contact_phone")
    op.drop_column("user", "emergency_contact_name")
    op.drop_column("user", "phone")
    op.drop_column("user", "date_of_birth")
