"""every account gains the cave and ice tags

The two words `app.services.default_tags.DEFAULT_TAGS` gained after `7025991d9229` seeded the
first six, given to the accounts that already exist on that revision's terms: a frozen copy,
a name the account holds under any case skipped, soft-deleted accounts included, nothing done
offline.

Revision ID: f3f873a94d1c
Revises: ea26677dea89
Create Date: 2026-10-14 13:12:22.338740

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.engine import Connection

# revision identifiers, used by Alembic.
revision: str = "f3f873a94d1c"
down_revision: str | None = "ea26677dea89"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_TAGS: tuple[str, ...] = ("cave", "ice")

_INSERT_MISSING = sa.text(
    """
    INSERT INTO tag (user_id, name, uuid, created_at)
    SELECT u.id, d.name, uuidv7(), now()
    FROM "user" AS u CROSS JOIN unnest(CAST(:names AS varchar[])) AS d(name)
    WHERE NOT EXISTS (
        SELECT 1 FROM tag AS t
        WHERE t.user_id = u.id
          AND casefold(t.name::text COLLATE pg_unicode_fast) = casefold(d.name::text COLLATE pg_unicode_fast)
    )
    """
)


def _backfill_default_tags(connection: Connection) -> int:
    """Give every account the default tags it lacks, and answer how many rows that was."""
    return connection.execute(_INSERT_MISSING, {"names": list(_DEFAULT_TAGS)}).rowcount


def upgrade() -> None:
    if not context.is_offline_mode():
        _backfill_default_tags(op.get_bind())


def downgrade() -> None:
    # Nothing, as in `7025991d9229`: once written these are the diver's own tags.
    pass
