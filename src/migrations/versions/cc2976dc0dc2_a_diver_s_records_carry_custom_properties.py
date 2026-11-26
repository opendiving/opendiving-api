"""a diver's records carry custom properties

Revision ID: cc2976dc0dc2
Revises: 28d1ff486573
Create Date: 2026-11-17 08:17:49.058200

One JSON column on each of the eight tables holding a record a diver owns, and on
`saved_site` for a dive site, every existing row starting with none: the server default writes
`[]` as the column is added.

A dive read gained `custom_properties`, and an import's `review` stores dive reads and is served as
stored. This writes `[]` into every dive of every review stored at shape 3 - what a read made then
would have given, no dive having had a property before this revision - and records shape 4.
`downgrade()` leaves the reviews: one carrying a member more is still what the earlier build serves.

**Hidden dive-form sets gain `custom_properties` wherever they hide `sightings`**, in
`DiveFormField`'s canonical position, so the Basic preset and any set built from it hide the new
field, as `b5dad8793a54` did for `people`. Every other set is left alone, and `downgrade()` takes
the member out of every set.
"""

import json
from collections.abc import Callable, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "cc2976dc0dc2"
down_revision: str | None = "28d1ff486573"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (table, column) holding a hidden dive-form set, as a JSON list of `DiveFormField` values.
_HIDDEN_SETS = (("dive_form_preset", "hidden_fields"), ("user", "dive_form_hidden_fields"))
_SIGHTINGS = "sightings"
_CUSTOM_PROPERTIES = "custom_properties"
# The members `DiveFormField` declares after it, which a canonical set lists after it too.
_DECLARED_AFTER = (
    "mixture.gear_item_uuid",
    "mixture.po2_limit",
    "mixture.helium",
    "mixture.start_pressure",
    "mixture.end_pressure",
    "mixture.role",
    "mixture.usage",
    "file_import",
)


def with_custom_properties(hidden: list[str]) -> list[str]:
    """The set with `custom_properties` in its canonical place, where it hides `sightings`."""
    if _SIGHTINGS not in hidden or _CUSTOM_PROPERTIES in hidden:
        return hidden
    at = next((index for index, field in enumerate(hidden) if field in _DECLARED_AFTER), len(hidden))
    return [*hidden[:at], _CUSTOM_PROPERTIES, *hidden[at:]]


def _without_custom_properties(hidden: list[str]) -> list[str]:
    return [field for field in hidden if field != _CUSTOM_PROPERTIES]


def _rewrite_hidden_sets(rewrite: Callable[[list[str]], list[str]], only_containing: str) -> None:
    """Apply `rewrite` to every hidden set naming `only_containing`, in one pass per table."""
    connection = op.get_bind()
    for table, column in _HIDDEN_SETS:
        rows = connection.execute(
            sa.text(f'SELECT id, {column} FROM "{table}" WHERE {column}::jsonb ? :key'),
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
                sa.text(f'UPDATE "{table}" SET {column} = CAST(:hidden AS json) WHERE id = :id'),
                changed,
            )


def with_no_properties(review: dict[str, Any]) -> dict[str, Any]:
    """`review` with `custom_properties` set to `[]` on each dive it stores. In place."""
    for row in review.get("dives") or []:
        dive = row.get("dive") if isinstance(row, dict) else None
        if isinstance(dive, dict):
            dive.setdefault("custom_properties", [])
    return review


def upgrade() -> None:
    op.add_column("certification", sa.Column("custom_properties", sa.JSON(), server_default="[]", nullable=False))
    op.add_column("contact", sa.Column("custom_properties", sa.JSON(), server_default="[]", nullable=False))
    op.add_column("course", sa.Column("custom_properties", sa.JSON(), server_default="[]", nullable=False))
    op.add_column("dive", sa.Column("custom_properties", sa.JSON(), server_default="[]", nullable=False))
    op.add_column("gear_item", sa.Column("custom_properties", sa.JSON(), server_default="[]", nullable=False))
    op.add_column("gear_set", sa.Column("custom_properties", sa.JSON(), server_default="[]", nullable=False))
    op.add_column("person", sa.Column("custom_properties", sa.JSON(), server_default="[]", nullable=False))
    op.add_column("saved_site", sa.Column("custom_properties", sa.JSON(), server_default="[]", nullable=False))
    op.add_column("trip", sa.Column("custom_properties", sa.JSON(), server_default="[]", nullable=False))

    if context.is_offline_mode():
        return
    _rewrite_hidden_sets(with_custom_properties, only_containing=_SIGHTINGS)
    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, review FROM logbook_import WHERE review_shape = 3 ORDER BY id").columns(
            id=sa.Integer, review=sa.JSON
        )
    ).all()
    update = sa.text("UPDATE logbook_import SET review = :review, review_shape = 4 WHERE id = :id").bindparams(
        sa.bindparam("review", type_=sa.JSON)
    )
    for row in rows:
        connection.execute(update, {"id": row.id, "review": with_no_properties(row.review)})


def downgrade() -> None:
    if not context.is_offline_mode():
        _rewrite_hidden_sets(_without_custom_properties, only_containing=_CUSTOM_PROPERTIES)
    op.drop_column("trip", "custom_properties")
    op.drop_column("saved_site", "custom_properties")
    op.drop_column("person", "custom_properties")
    op.drop_column("gear_set", "custom_properties")
    op.drop_column("gear_item", "custom_properties")
    op.drop_column("dive", "custom_properties")
    op.drop_column("course", "custom_properties")
    op.drop_column("contact", "custom_properties")
    op.drop_column("certification", "custom_properties")
