"""the check-in details take DiveJSON's bounds

Revision ID: b7c41e9d2a05
Revises: 9ec57e8a5182
Create Date: 2026-09-23 20:10:00.000000

Three `user` columns widen to the bound DiveJSON §6.1 gives the member each travels as: the
emergency contact's name and the insurance provider to 255, the contact's relationship to 64.
An import writes what a conforming document carries, and a column narrower than the format
would refuse a value the document is entitled to hold.

Widening a `VARCHAR` rewrites nothing and every stored value already fits, so no row needs a
data path. `downgrade()` narrows them back and fails on a value longer than the old width,
which is the honest answer: there is nowhere shorter to put it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c41e9d2a05"
down_revision: str | None = "9ec57e8a5182"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("user", "emergency_contact_name", existing_type=sa.String(length=100), type_=sa.String(length=255))
    op.alter_column(
        "user", "emergency_contact_relationship", existing_type=sa.String(length=50), type_=sa.String(length=64)
    )
    op.alter_column("user", "insurance_provider", existing_type=sa.String(length=100), type_=sa.String(length=255))


def downgrade() -> None:
    op.alter_column("user", "insurance_provider", existing_type=sa.String(length=255), type_=sa.String(length=100))
    op.alter_column(
        "user", "emergency_contact_relationship", existing_type=sa.String(length=64), type_=sa.String(length=50)
    )
    op.alter_column("user", "emergency_contact_name", existing_type=sa.String(length=255), type_=sa.String(length=100))
