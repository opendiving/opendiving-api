"""a sighting carries an abundance band beside its count

Revision ID: 671e5dc854e0
Revises: e41fcd2a6308
Create Date: 2026-10-14 12:00:00.000000

`dive_species.abundance` is DiveJSON's sighting `abundance`: `few`, `many` or `abundant`, the
diver's order-of-magnitude reading where there is no count. Nullable with no default and no
`CHECK` on the vocabulary, as `dive.current` stores its own.

`ck_dive_species_count_or_abundance` holds that a sighting carries at most one of the two. It
needs no repair pass: the column is new, so every stored row has no band and satisfies it, and
the outgoing build's insert names neither column.

`downgrade()` drops the constraint and the column, which discards every band.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "671e5dc854e0"
down_revision: str | None = "e41fcd2a6308"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EXCLUSION = "ck_dive_species_count_or_abundance"


def upgrade() -> None:
    op.add_column("dive_species", sa.Column("abundance", sa.String(length=16), nullable=True))
    op.create_check_constraint(_EXCLUSION, "dive_species", "count IS NULL OR abundance IS NULL")


def downgrade() -> None:
    op.drop_constraint(_EXCLUSION, "dive_species", type_="check")
    op.drop_column("dive_species", "abundance")
