"""a trip is a sequence of dated parts

Revision ID: 7c4be0a1d385
Revises: c985d7b26605
Create Date: 2026-09-20 11:40:00.000000

`trip_part` replaces `trip_location`, and a trip's two date columns move onto it. A trip
used to be one span with an ordered list of placeless names beside it; it is now an
ordered list of parts, each carrying its own optional range and its own optional place.

**The data move is hand-written and autogenerate never saw it.** Autogenerate drafts the
table and the dropped columns; the two `INSERT ... SELECT`s below are the whole of what
makes the revision safe, and they are what a self-hoster runs against a logbook that is
already theirs.

**`trip_part` is created outright rather than renamed**, which matters beyond taste.
`tests/test_migrations.py` subtracts every `CREATE TABLE` in the offline SQL from
`Base.metadata.tables`, so a table that only ever arrived by `ALTER TABLE ... RENAME TO`
would fail that guard for the life of the repository. Creating it fresh also gives the
primary key, the sequence and the foreign key their own `trip_part_*` names instead of
leaving `trip_location_*` ones behind, which a rename does not carry.

**Every migrated trip keeps its span.** For a trip with places, the earliest part by
position takes the trip's `start_date` and the latest takes its `end_date`; a trip with no
place at all gets one part carrying both dates and no name. So for every trip, the
earliest `start_date` and the latest `end_date` across its parts after this revision equal
the trip's `start_date` and `end_date` before it.

**`downgrade()` undoes the copy, not just the DDL.** A part with no place has no
representation in `trip_location`, whose `name` is `NOT NULL`, so a downgrade that only
moved the rows back would fail on this migration's own output. It refills `trip.start_date`
and `trip.end_date` from each trip's parts, copies back only the parts that have a place,
and then tightens `start_date`. That last `ALTER` **fails if any trip has no dated part**,
which is the intended outcome rather than an oversight: `trip.start_date` was `NOT NULL`
and there is no honest date to invent - the same refusal
`9f55a06b2f49_a_course_need_not_name_an_agency.py` makes for an agency. The whole revision
is one transaction, so the database is left exactly as it was.

The downgrade is therefore exactly reversing for this revision's own output, and lossy only
for placeless parts a diver added afterwards.

**Offline rendering.** `alembic upgrade head --sql` runs against no database, and both data
moves are single `INSERT ... SELECT` statements, so they render into the script rather than
being skipped: an operator applying the SQL by hand gets the backfill too.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c4be0a1d385"
down_revision: str | None = "c985d7b26605"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# One part per existing location, with the trip's span spread across the ends. `position`
# carries over unchanged, so the diver's order survives. The window is ordered by
# `(position, id)` rather than `position` alone because nothing ever enforced uniqueness on
# it, and a tie would otherwise put the start date on an arbitrary one of two rows.
_COPY_LOCATIONS = sa.text(
    """
    INSERT INTO trip_part (
        trip_id, position, start_date, end_date, name, display_name,
        latitude, longitude, bbox_south, bbox_north, bbox_west, bbox_east
    )
    SELECT trip_id,
           position,
           CASE WHEN ordinal = 1 THEN trip_start_date END,
           CASE WHEN ordinal = total THEN trip_end_date END,
           name,
           display_name,
           latitude,
           longitude,
           bbox_south,
           bbox_north,
           bbox_west,
           bbox_east
    FROM (
        SELECT l.trip_id,
               l.position,
               l.name,
               l.display_name,
               l.latitude,
               l.longitude,
               l.bbox_south,
               l.bbox_north,
               l.bbox_west,
               l.bbox_east,
               t.start_date AS trip_start_date,
               t.end_date AS trip_end_date,
               row_number() OVER (PARTITION BY l.trip_id ORDER BY l.position, l.id) AS ordinal,
               count(*) OVER (PARTITION BY l.trip_id) AS total
        FROM trip_location AS l
        JOIN trip AS t ON t.id = l.trip_id
    ) AS ranked
    """
)

# The majority shape in the development corpus: a trip nobody ever named a place for. It
# becomes one part with both dates and no name, which is what a part with no place is.
_DATES_FOR_PLACELESS_TRIPS = sa.text(
    """
    INSERT INTO trip_part (trip_id, position, start_date, end_date)
    SELECT t.id, 0, t.start_date, t.end_date
    FROM trip AS t
    WHERE NOT EXISTS (SELECT 1 FROM trip_location AS l WHERE l.trip_id = t.id)
    """
)

# Back the other way: each trip's span is the span of its parts.
_REFILL_TRIP_DATES = sa.text(
    """
    UPDATE trip AS t
    SET start_date = spans.start_date,
        end_date = spans.end_date
    FROM (
        SELECT trip_id, min(start_date) AS start_date, max(end_date) AS end_date
        FROM trip_part
        GROUP BY trip_id
    ) AS spans
    WHERE spans.trip_id = t.id
    """
)

# Only the parts that have a place: a placeless one has no row to become, `name` being
# `NOT NULL` over there. `position` carries over as it stands rather than being re-numbered,
# so a gap left by a dropped placeless part preserves the order of what remains.
_COPY_BACK_PLACES = sa.text(
    """
    INSERT INTO trip_location (
        trip_id, name, position, display_name,
        latitude, longitude, bbox_south, bbox_north, bbox_west, bbox_east
    )
    SELECT trip_id, name, position, display_name,
           latitude, longitude, bbox_south, bbox_north, bbox_west, bbox_east
    FROM trip_part
    WHERE name IS NOT NULL
    """
)


def upgrade() -> None:
    op.create_table(
        "trip_part",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trip_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("display_name", sa.String(length=512), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("bbox_south", sa.Float(), nullable=True),
        sa.Column("bbox_north", sa.Float(), nullable=True),
        sa.Column("bbox_west", sa.Float(), nullable=True),
        sa.Column("bbox_east", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["trip_id"], ["trip.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trip_part_trip_id_position", "trip_part", ["trip_id", "position"], unique=False)

    op.execute(_COPY_LOCATIONS)
    op.execute(_DATES_FOR_PLACELESS_TRIPS)

    op.drop_index("ix_trip_location_trip_id_position", table_name="trip_location")
    op.drop_table("trip_location")

    # After the copy, and in this order: the index is on the column about to go.
    op.drop_index("ix_trip_user_id_start_date", table_name="trip")
    op.drop_column("trip", "start_date")
    op.drop_column("trip", "end_date")


def downgrade() -> None:
    op.create_table(
        "trip_location",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trip_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=512), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("bbox_south", sa.Float(), nullable=True),
        sa.Column("bbox_north", sa.Float(), nullable=True),
        sa.Column("bbox_west", sa.Float(), nullable=True),
        sa.Column("bbox_east", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["trip_id"], ["trip.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trip_location_trip_id_position", "trip_location", ["trip_id", "position"], unique=False)

    # Nullable to begin with: the values that will fill `start_date` are still in
    # `trip_part`, and the column cannot be `NOT NULL` before the UPDATE below has run.
    op.add_column("trip", sa.Column("start_date", sa.Date(), nullable=True))
    op.add_column("trip", sa.Column("end_date", sa.Date(), nullable=True))

    op.execute(_REFILL_TRIP_DATES)
    op.execute(_COPY_BACK_PLACES)

    op.drop_index("ix_trip_part_trip_id_position", table_name="trip_part")
    op.drop_table("trip_part")

    # Refuses a trip left with no date at all - see the module docstring.
    op.alter_column("trip", "start_date", existing_type=sa.Date(), nullable=False)
    op.create_index(
        "ix_trip_user_id_start_date", "trip", ["user_id", sa.literal_column("start_date DESC")], unique=False
    )
