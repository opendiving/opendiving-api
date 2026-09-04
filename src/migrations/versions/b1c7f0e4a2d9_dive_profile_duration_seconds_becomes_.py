"""dive_profile.duration_seconds becomes duration

The JSON export is now a DiveJSON 1.0 document, whose profile object names its span
`duration` (spec §6.4). That member is served straight off this column through
`DiveProfileRead`, so storage speaks the same word rather than translating one name into
another at every read - see *"The profile speaks one vocabulary, storage included"* in
DECISIONS.md.

A rename, not a drop-and-add: `ALTER TABLE ... RENAME COLUMN` keeps every row's value and
the column's `NOT NULL`, which is the whole point - these are extracted samples nobody can
re-derive without the original dive-computer file. Autogenerate renders a rename as a drop
plus an add and would have silently emptied the column, which is why this file is
hand-written (AGENTS.md, *Schema changes*).

Nothing else about the table moves. The JSONB `data` payload keeps its compact `t`/`v`
keys, deliberately: they are not on the wire, and `to_read_schema` in
`services/dive_profiles.py` is the one place that maps them onto DiveJSON's
`times`/`values`.

Revision ID: b1c7f0e4a2d9
Revises: 84bee1255635
Create Date: 2026-09-04 12:10:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c7f0e4a2d9"
down_revision: str | None = "84bee1255635"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("dive_profile", "duration_seconds", new_column_name="duration")


def downgrade() -> None:
    op.alter_column("dive_profile", "duration", new_column_name="duration_seconds")
