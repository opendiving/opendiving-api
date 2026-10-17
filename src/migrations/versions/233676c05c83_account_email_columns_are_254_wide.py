"""account email columns are 254 wide

Revision ID: 233676c05c83
Revises: f3f873a94d1c
Create Date: 2026-10-17 07:39:53.695510

Every column holding an account's address was `VARCHAR(50)`, while every schema writing one
admits what `EmailStr` does - up to 254 characters - so a valid longer address reached the
INSERT and came back a 500. All five widen to `EMAIL_MAX_LENGTH`. Raising a `VARCHAR` limit
is a catalog change in Postgres: no table or index rewrite, and no row changes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "233676c05c83"
down_revision: str | None = "f3f873a94d1c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = (
    ("auth_audit_event", True),
    ("authentication_request", False),
    ("invitation", False),
    ("invite_request", False),
    ("user", False),
)


def upgrade() -> None:
    for table, nullable in COLUMNS:
        op.alter_column(
            table, "email", existing_type=sa.VARCHAR(length=50), type_=sa.String(length=254), existing_nullable=nullable
        )


def downgrade() -> None:
    # Fails on any address longer than 50 rather than truncating it: a cut address is a
    # different identity, and `user.email` is unique.
    for table, nullable in COLUMNS:
        op.alter_column(
            table, "email", existing_type=sa.String(length=254), type_=sa.VARCHAR(length=50), existing_nullable=nullable
        )
