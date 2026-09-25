"""a dive may carry only its date

Revision ID: ab9a5add4fee
Revises: ce09bc7d4c64
Create Date: 2026-09-25 11:05:13.620369

DiveJSON lets a dive's `started_at` be a bare date: the day was recorded and the time of day
was not. `dive.start_date_only` marks that state, false for every existing row, and a date-only
dive can carry no offset. Nothing to backfill: no dive could hold the state before this.

`downgrade()` refuses while any dive is date-only, since the schema below this one has no
spelling for it and would read the stored midnight as a time somebody recorded.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "ab9a5add4fee"
down_revision: str | None = "ce09bc7d4c64"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("dive", sa.Column("start_date_only", sa.Boolean(), server_default="false", nullable=False))
    op.create_check_constraint(
        "ck_dive_start_date_only_has_no_offset", "dive", "NOT start_date_only OR utc_offset_minutes IS NULL"
    )


def downgrade() -> None:
    if not context.is_offline_mode():
        count = op.get_bind().execute(sa.text("SELECT count(*) FROM dive WHERE start_date_only")).scalar_one()
        if count:
            raise RuntimeError(
                f"{count} dive(s) record a date and no time of day, which the schema below this revision cannot "
                "hold without inventing a midnight. This downgrade refuses rather than invent one."
            )
    op.drop_constraint("ck_dive_start_date_only_has_no_offset", "dive", type_="check")
    op.drop_column("dive", "start_date_only")
