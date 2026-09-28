"""a sighting carries a count and a note

Revision ID: c47b308253a3
Revises: b5dad8793a54
Create Date: 2026-09-27 16:00:00.000000

`dive_species` is a dive's sightings, and each gains how many were counted and what the diver
wrote about it: DiveJSON's Sighting, which replaced the dive's bare `species_uuids` list.

`count` is nullable - null is *seen, not counted*, which is not `1` - and
`ck_dive_species_count_positive` is written by hand, as `1afde4812cf3` wrote its constraint.
It needs no repair pass: the column is new and no row holds a value.

`notes` is `NOT NULL` with an empty-string server default, which is kept rather than dropped
once the column is filled, as `60ec1a2894ea` drops its own. The table has rows, and between
this revision running and traffic moving to the new build the outgoing build keeps inserting
sightings with no note; the default is what fills those rows.

**Hidden dive-form sets rename `species_uuids` to `sightings` in place.** A preset or an
account's own set is stored data in `DiveFormField` declaration order, and the new member
takes the old one's slot, so the rename keeps every set canonical without this file carrying
a frozen copy of the enum. `e0cfbd603859`'s frozen copy of the default presets is left as it
is - see *"The revision that backfills presets carries its own frozen copy of them"* in
DECISIONS.md.

**Offline rendering.** `tests/test_migrations.py` runs `upgrade head --sql` against no
database, so the data step is guarded with `context.is_offline_mode()`.

`downgrade()` renames `sightings` back to `species_uuids` in every hidden set and drops the
two columns, which discards every count and note - by construction, since nothing else in the
schema carries either.
"""

import json
from collections.abc import Callable, Sequence

import sqlalchemy as sa
from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "c47b308253a3"
down_revision: str | None = "b5dad8793a54"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COUNT_CONSTRAINT = "ck_dive_species_count_positive"

# (table, column) holding a hidden dive-form set, as a JSON list of `DiveFormField` values.
_HIDDEN_SETS = (("dive_form_preset", "hidden_fields"), ("user", "dive_form_hidden_fields"))
_RETIRED = "species_uuids"
_RENAMED = "sightings"


def _renaming(old: str, new: str) -> Callable[[list[str]], list[str]]:
    """A rewrite putting `new` where `old` stood, which keeps a canonical set canonical."""

    def rename(hidden: list[str]) -> list[str]:
        if new in hidden:
            return [field for field in hidden if field != old]
        return [new if field == old else field for field in hidden]

    return rename


def _rewrite_hidden_sets(rewrite: Callable[[list[str]], list[str]], only_containing: str) -> None:
    """Apply `rewrite` to every hidden set naming `only_containing`, in one pass per table."""
    connection = op.get_bind()
    for table, column in _HIDDEN_SETS:
        rows = connection.execute(
            sa.text(f'SELECT id, {column} FROM "{table}" WHERE {column}::jsonb ? :key'),  # noqa: S608 - literals above
            {"key": only_containing},
        ).all()
        changed = []
        for row_id, stored in rows:
            # A driver without a json codec hands back the text.
            hidden = json.loads(stored) if isinstance(stored, str) else stored
            if (new := rewrite(hidden)) != hidden:
                changed.append({"id": row_id, "hidden": json.dumps(new)})
        if changed:
            connection.execute(
                sa.text(f'UPDATE "{table}" SET {column} = CAST(:hidden AS json) WHERE id = :id'),  # noqa: S608
                changed,
            )


def upgrade() -> None:
    op.add_column("dive_species", sa.Column("count", sa.Integer(), nullable=True))
    op.add_column("dive_species", sa.Column("notes", sa.Text(), server_default="", nullable=False))
    op.create_check_constraint(_COUNT_CONSTRAINT, "dive_species", "count IS NULL OR count >= 1")

    if not context.is_offline_mode():
        _rewrite_hidden_sets(_renaming(_RETIRED, _RENAMED), only_containing=_RETIRED)


def downgrade() -> None:
    if not context.is_offline_mode():
        _rewrite_hidden_sets(_renaming(_RENAMED, _RETIRED), only_containing=_RENAMED)

    op.drop_constraint(_COUNT_CONSTRAINT, "dive_species", type_="check")
    op.drop_column("dive_species", "notes")
    op.drop_column("dive_species", "count")
