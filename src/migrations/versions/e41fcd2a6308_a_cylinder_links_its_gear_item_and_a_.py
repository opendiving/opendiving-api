"""a cylinder links its gear item, and a service rule can run on hours

Revision ID: e41fcd2a6308
Revises: 9d5f74172b52
Create Date: 2026-10-13 06:41:06.359323

`dive_mixture.gear_item_id` links a dive's cylinder to the diver's gear item it is, unique per
dive where set; a gear item gains its vessel's `volume`, `working_pressure` and `material` and a
derived `dive_time`, the summed `submerged_time` of the dives that used it; a service schedule
gains a third interval arm, `interval_hours`, with its snapshot, threshold and notify column; a
service record gains `dive_time_at_service`. Autogenerate drafted the columns; the constraints
and the data steps are hand-written and it sees none of them.

**`dive_time` is filled for every gear item**, by the recount's own sum over the item's live
dives. Without it every item would read a dive time of zero until its owner next saved a dive.
No dive links a vessel yet, so the sum runs over `dive_gear_item` alone and `dive_count` stands.

**Both time snapshots are filled from the dive history**, as the figure the live path would have
snapshotted when the row was written: the summed `submerged_time` of the item's live dives
created at or before the schedule (`dive_time_at_start`) or the record (`dive_time_at_service`).
A dive created before the row and linked after it is counted as if linked then, since the link's
own date is not kept; a dive deleted or unlinked since is no longer seen. Left at 0, an hours arm
added to an existing schedule would count the item's whole life.

**The has-an-interval `CHECK` is restated with three arms.** Autogenerate cannot see a changed
text under an unchanged name (`d3b1700eb489`).

**Hidden dive-form sets gain `mixture.gear_item_uuid` wherever they hide
`mixture.start_pressure`**, the contact key's precedent (`a9b7dc451f00`): the new member is
declared first among the per-cylinder keys, so it is inserted before the set's first of them.

**Offline rendering.** The data steps are guarded with `context.is_offline_mode()`.

`downgrade()` drops what this adds and removes the key from every hidden set; the links, the
vessel facts and every hours arm are lost. A schedule whose only interval is hours cannot
survive the restated `CHECK`, so it goes first.
"""

import json
from collections.abc import Callable, Sequence

import sqlalchemy as sa
from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "e41fcd2a6308"
down_revision: str | None = "9d5f74172b52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HAS_AN_INTERVAL = "ck_gear_service_schedule_has_an_interval"
_THREE_ARMS = "interval_months IS NOT NULL OR interval_dives IS NOT NULL OR interval_hours IS NOT NULL"
_TWO_ARMS = "interval_months IS NOT NULL OR interval_dives IS NOT NULL"

_FILL_DIVE_TIME = sa.text(
    """
    UPDATE gear_item
    SET dive_time = sums.total
    FROM (
        SELECT dgi.gear_item_id, SUM(dive.submerged_time) AS total
        FROM dive_gear_item AS dgi
        JOIN dive ON dive.id = dgi.dive_id
        WHERE dive.is_deleted = false AND dive.submerged_time IS NOT NULL
        GROUP BY dgi.gear_item_id
    ) AS sums
    WHERE sums.gear_item_id = gear_item.id
    """
)

# `{table}`/`{column}` are this file's literals below. The dive must belong to the row's owner,
# as the recount only ever counts the owner's dives.
_FILL_SNAPSHOT = """
    UPDATE {table} AS host
    SET {column} = sums.total
    FROM (
        SELECT host.id, SUM(dive.submerged_time) AS total
        FROM {table} AS host
        JOIN dive_gear_item AS dgi ON dgi.gear_item_id = host.gear_item_id
        JOIN dive ON dive.id = dgi.dive_id
        WHERE dive.user_id = host.user_id
          AND dive.is_deleted = false
          AND dive.submerged_time IS NOT NULL
          AND dive.created_at <= host.created_at
        GROUP BY host.id
    ) AS sums
    WHERE sums.id = host.id
"""

# (table, column) holding a hidden dive-form set, as a JSON list of `DiveFormField` values.
_HIDDEN_SETS = (("dive_form_preset", "hidden_fields"), ("user", "dive_form_hidden_fields"))
_START_PRESSURE = "mixture.start_pressure"
_GEAR_ITEM = "mixture.gear_item_uuid"


def _with_gear_item(hidden: list[str]) -> list[str]:
    """The set with the vessel key placed before its first per-cylinder key."""
    if _START_PRESSURE not in hidden or _GEAR_ITEM in hidden:
        return hidden
    at = next(index for index, key in enumerate(hidden) if key.startswith("mixture."))
    return [*hidden[:at], _GEAR_ITEM, *hidden[at:]]


def _without_gear_item(hidden: list[str]) -> list[str]:
    return [key for key in hidden if key != _GEAR_ITEM]


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
    op.add_column("dive_mixture", sa.Column("gear_item_id", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_dive_mixture_gear_item_id"), "dive_mixture", ["gear_item_id"], unique=False)
    op.create_index(
        "ux_dive_mixture_dive_id_gear_item_id",
        "dive_mixture",
        ["dive_id", "gear_item_id"],
        unique=True,
        postgresql_where=sa.text("gear_item_id IS NOT NULL"),
    )
    op.create_foreign_key(
        "dive_mixture_gear_item_id_fkey", "dive_mixture", "gear_item", ["gear_item_id"], ["id"], ondelete="SET NULL"
    )

    op.add_column("gear_item", sa.Column("dive_time", sa.BigInteger(), server_default="0", nullable=False))
    op.add_column("gear_item", sa.Column("volume", sa.Float(), nullable=True))
    op.add_column("gear_item", sa.Column("working_pressure", sa.Float(), nullable=True))
    op.add_column("gear_item", sa.Column("material", sa.String(length=16), nullable=True))
    op.create_check_constraint("ck_gear_item_volume_positive", "gear_item", "volume IS NULL OR volume > 0")
    op.create_check_constraint(
        "ck_gear_item_working_pressure_range",
        "gear_item",
        "working_pressure IS NULL OR (working_pressure > 0 AND working_pressure <= 350)",
    )

    op.add_column(
        "gear_service_record", sa.Column("dive_time_at_service", sa.BigInteger(), server_default="0", nullable=False)
    )
    op.add_column("gear_service_schedule", sa.Column("interval_hours", sa.Integer(), nullable=True))
    op.add_column(
        "gear_service_schedule", sa.Column("dive_time_at_start", sa.BigInteger(), server_default="0", nullable=False)
    )
    op.add_column("gear_service_schedule", sa.Column("next_due_at_dive_time", sa.BigInteger(), nullable=True))
    op.add_column("gear_service_schedule", sa.Column("notified_for_due_at_dive_time", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        "ck_gear_service_schedule_interval_hours_positive",
        "gear_service_schedule",
        "interval_hours IS NULL OR interval_hours > 0",
    )
    op.drop_constraint(_HAS_AN_INTERVAL, "gear_service_schedule", type_="check")
    op.create_check_constraint(_HAS_AN_INTERVAL, "gear_service_schedule", _THREE_ARMS)

    if not context.is_offline_mode():
        connection = op.get_bind()
        connection.execute(_FILL_DIVE_TIME)
        connection.execute(sa.text(_FILL_SNAPSHOT.format(table="gear_service_schedule", column="dive_time_at_start")))
        connection.execute(sa.text(_FILL_SNAPSHOT.format(table="gear_service_record", column="dive_time_at_service")))
        _rewrite_hidden_sets(_with_gear_item, only_containing=_START_PRESSURE)


def downgrade() -> None:
    if not context.is_offline_mode():
        _rewrite_hidden_sets(_without_gear_item, only_containing=_GEAR_ITEM)
        op.get_bind().execute(sa.text(f"DELETE FROM gear_service_schedule WHERE NOT ({_TWO_ARMS})"))  # noqa: S608

    op.drop_constraint(_HAS_AN_INTERVAL, "gear_service_schedule", type_="check")
    op.create_check_constraint(_HAS_AN_INTERVAL, "gear_service_schedule", _TWO_ARMS)
    op.drop_constraint("ck_gear_service_schedule_interval_hours_positive", "gear_service_schedule", type_="check")
    op.drop_column("gear_service_schedule", "notified_for_due_at_dive_time")
    op.drop_column("gear_service_schedule", "next_due_at_dive_time")
    op.drop_column("gear_service_schedule", "dive_time_at_start")
    op.drop_column("gear_service_schedule", "interval_hours")
    op.drop_column("gear_service_record", "dive_time_at_service")

    op.drop_constraint("ck_gear_item_working_pressure_range", "gear_item", type_="check")
    op.drop_constraint("ck_gear_item_volume_positive", "gear_item", type_="check")
    op.drop_column("gear_item", "material")
    op.drop_column("gear_item", "working_pressure")
    op.drop_column("gear_item", "volume")
    op.drop_column("gear_item", "dive_time")

    op.drop_constraint("dive_mixture_gear_item_id_fkey", "dive_mixture", type_="foreignkey")
    op.drop_index(
        "ux_dive_mixture_dive_id_gear_item_id",
        table_name="dive_mixture",
        postgresql_where=sa.text("gear_item_id IS NOT NULL"),
    )
    op.drop_index(op.f("ix_dive_mixture_gear_item_id"), table_name="dive_mixture")
    op.drop_column("dive_mixture", "gear_item_id")
