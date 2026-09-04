"""total dive time is a sum, so it is a bigint

Revision ID: 3818275fcfcd
Revises: b1dbbf0d263e
Create Date: 2026-09-04 13:50:08.532922

`user_dive_stats.total_time` is `SUM(dive.duration)` over every non-deleted dive an account
holds, and it was the same 32-bit `Integer` as the column it sums. A handful of dives near
`dive.duration`'s own ceiling therefore overflow it - and the write that fails is this one,
inside whatever transaction recomputed the stats, which for a logbook import means an entire
restore refused over an arithmetic overflow in a dashboard tile.

It is the only counter on that table that is a sum of caller-supplied values rather than a
count of rows, which is why it is the only one that widens. The importer separately caps a
single dive's duration at a year, so the overflow is implausible; this is what makes it
impossible.

Widening is not a rewrite: Postgres does `integer` -> `bigint` as a table rewrite with no
data loss, and this table holds one row per account.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3818275fcfcd"
down_revision: str | None = "b1dbbf0d263e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "user_dive_stats", "total_time", existing_type=sa.INTEGER(), type_=sa.BigInteger(), existing_nullable=False
    )


def downgrade() -> None:
    # Lossy where a real overflow is what motivated the widening, which is the honest shape
    # of undoing it: an account whose total exceeds the narrower column cannot be narrowed
    # back, so its total is clamped to the largest a 32-bit `Integer` holds. Nothing reads
    # this column but a dashboard tile, and the next `recalculate_dive_stats` would recompute
    # it - into the same overflow.
    op.execute("UPDATE user_dive_stats SET total_time = 2147483647 WHERE total_time > 2147483647")
    op.alter_column(
        "user_dive_stats", "total_time", existing_type=sa.BigInteger(), type_=sa.INTEGER(), existing_nullable=False
    )
