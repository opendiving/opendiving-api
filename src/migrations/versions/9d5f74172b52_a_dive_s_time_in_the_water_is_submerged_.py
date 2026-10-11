"""a dive's time in the water is submerged_time

DiveJSON names the dive's figure `submerged_time`, so the column and the stats total that
sums it take the same name: `dive.duration` becomes `dive.submerged_time` and
`user_dive_stats.total_time` becomes `user_dive_stats.total_submerged_time`.

Hand-written because autogenerate renders a rename as a drop plus an add, which would empty
both columns. `RENAME COLUMN` keeps every value and the `NOT NULL`, and Postgres rewrites the
CHECK expression and the `INCLUDE` list of `ix_dive_user_id_stats` to the new name on its
own; only the constraint's name would stay wrong, so it is dropped and recreated under the
new one, which re-validates rows that already satisfy it.

Revision ID: 9d5f74172b52
Revises: f3a9c1d27b64
Create Date: 2026-10-11 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9d5f74172b52"
down_revision: str | None = "f3a9c1d27b64"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("dive", "duration", new_column_name="submerged_time")
    op.drop_constraint("ck_dive_duration_positive", "dive", type_="check")
    op.create_check_constraint("ck_dive_submerged_time_positive", "dive", "submerged_time > 0")
    op.alter_column("user_dive_stats", "total_time", new_column_name="total_submerged_time")


def downgrade() -> None:
    op.alter_column("user_dive_stats", "total_submerged_time", new_column_name="total_time")
    op.drop_constraint("ck_dive_submerged_time_positive", "dive", type_="check")
    op.alter_column("dive", "submerged_time", new_column_name="duration")
    op.create_check_constraint("ck_dive_duration_positive", "dive", "duration > 0")
