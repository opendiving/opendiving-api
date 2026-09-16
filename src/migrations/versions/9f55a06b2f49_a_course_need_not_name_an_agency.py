"""a course need not name an agency

Revision ID: 9f55a06b2f49
Revises: 1afde4812cf3
Create Date: 2026-09-16 19:17:38.714661

`course.agency` becomes nullable, because a course taught by a private instructor runs
under no agency at all and the column had no way to say so. `certification.agency` stays
`NOT NULL`: a c-card is issued by somebody, and the format keeps that member REQUIRED.

A widening, so no existing row's value changes and there is nothing to repair first.

`downgrade()` re-adds the constraint and **fails if any course has no agency**, which is
the intended outcome rather than an oversight: there is no honest value to put there, and
inventing one would write a fabricated agency onto a diver's course - the failure this
change exists to end. The ALTER raises before it touches anything, so the database is left
as it was.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9f55a06b2f49"
down_revision: str | None = "1afde4812cf3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("course", "agency", existing_type=sa.VARCHAR(length=32), nullable=True)


def downgrade() -> None:
    op.alter_column("course", "agency", existing_type=sa.VARCHAR(length=32), nullable=False)
