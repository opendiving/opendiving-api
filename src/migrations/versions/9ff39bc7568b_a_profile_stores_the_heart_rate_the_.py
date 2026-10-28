"""a profile stores the heart rate the computer measured

The summary half of the `heart_rate` channel: its maximum, which is the channel's presence
test like every other extreme, and its mean, which the cap takes before it thins the series.
The samples go into the existing `data` payload and need no column.

**Nothing backfills them.** `PROFILE_EXTRACTOR_VERSION` goes to 10 in the same change, so
`backfill_dive_profiles` re-reads every file-backed profile on its next run, and no part of
this change runs it; until then a stored profile carries no heart rate and both columns stay
NULL. A migration could not do it in any case: the readings are in the files, in the blob
store.

`downgrade()` drops the columns and what they held.

Revision ID: 9ff39bc7568b
Revises: e1d1cde07998
Create Date: 2026-10-28 23:26:14.241195

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9ff39bc7568b"
down_revision: str | None = "e1d1cde07998"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("dive_profile", sa.Column("max_heart_rate_bpm", sa.Integer(), nullable=True))
    op.add_column("dive_profile", sa.Column("avg_heart_rate_bpm", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("dive_profile", "avg_heart_rate_bpm")
    op.drop_column("dive_profile", "max_heart_rate_bpm")
