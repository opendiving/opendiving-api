"""every account starts with the default tags

Registration seeds `app.services.default_tags.DEFAULT_TAGS`; this gives the accounts that
predate it the same words. No DDL. The list is a frozen copy rather than an import, as
`e0cfbd603859`'s presets are: a revision is history, and an import would rewrite it.

A name the account already holds, compared on `ux_tag_user_id_name_folded`'s own expression,
is skipped - a diver's `Night` stays theirs. Soft-deleted accounts are seeded too, as the
presets were, so one restored is not the only account without them. Skipped when rendering
SQL offline, where there is nothing to read.

Revision ID: 7025991d9229
Revises: 671e5dc854e0
Create Date: 2026-10-14 11:41:15.181562

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.engine import Connection

# revision identifiers, used by Alembic.
revision: str = "7025991d9229"
down_revision: str | None = "671e5dc854e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_TAGS: tuple[str, ...] = ("cleanup", "drift", "night", "photo", "survey", "wreck")

# `uuidv7()` keeps these rows time-ordered like every tag the ORM writes.
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
    # Nothing: once written these are the diver's own tags, indistinguishable from one they
    # typed, and may already be on dives.
    pass
