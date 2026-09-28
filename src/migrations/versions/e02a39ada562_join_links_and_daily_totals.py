"""join links and daily totals

Revision ID: e02a39ada562
Revises: 6849ff025422
Create Date: 2026-09-28 18:39:18.629639

`daily_total` is created and `accounts_created` backfilled from the accounts already stored,
one per `user` on its `created_at` UTC day: the earliest-created under `bootstrap`, one with
an accepted invitation for `lower(user.email)` under `invitation` - invitation rows are
lowercase and a Google address may not be - and the rest under `open`. Approximate by
construction: whether an invitation came from the waiting list was never stored, an account
whose address changed or whose inviter was purged has no accepted invitation under its
current address, and a purged first account leaves the earliest survivor as `bootstrap`.
The statement never lowers a stored count, so running it again changes nothing.

`authentication_request.via` is added empty and `invitation.from_invite_request` as `false`
for every row, which is also what the outgoing build's inserts get while a deploy overlaps
it: they name neither column. An account that build creates in the overlap is not counted.

`downgrade()` drops the table and both columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e02a39ada562"
down_revision: str | None = "6849ff025422"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here rather than imported: a revision is history and reads no live code, and a
# test runs this exact statement against a seeded database.
BACKFILL_ACCOUNTS_CREATED = """
INSERT INTO daily_total (day, metric, key, count)
SELECT (u.created_at AT TIME ZONE 'UTC')::date,
       'accounts_created',
       CASE
           WHEN u.id = (SELECT id FROM "user" ORDER BY created_at, id LIMIT 1) THEN 'bootstrap'
           WHEN EXISTS (
               SELECT 1 FROM invitation i WHERE i.email = lower(u.email) AND i.accepted_at IS NOT NULL
           ) THEN 'invitation'
           ELSE 'open'
       END,
       count(*)
FROM "user" u
GROUP BY 1, 2, 3
ON CONFLICT (day, metric, key) DO UPDATE SET count = GREATEST(daily_total.count, excluded.count)
"""


def upgrade() -> None:
    op.create_table(
        "daily_total",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=32), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("day", "metric", "key"),
    )
    op.execute(BACKFILL_ACCOUNTS_CREATED)
    op.add_column("authentication_request", sa.Column("via", sa.String(length=32), nullable=True))
    op.add_column("invitation", sa.Column("from_invite_request", sa.Boolean(), server_default="false", nullable=False))


def downgrade() -> None:
    op.drop_column("invitation", "from_invite_request")
    op.drop_column("authentication_request", "via")
    op.drop_table("daily_total")
