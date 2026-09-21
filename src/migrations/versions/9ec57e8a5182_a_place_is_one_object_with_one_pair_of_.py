"""a place is one object with one pair of names

Revision ID: 9ec57e8a5182
Revises: 7c4be0a1d385
Create Date: 2026-09-21 12:00:00.000000

A dive site's free-text `location` becomes the same place object a trip part already
carried, and both hosts settle on one pair of names: `name`, the place as a person writes
it, and `full_name`, the fullest form a lookup returned.

**Two tables move and the data move is hand-written.** Autogenerate drafts the columns and
sees none of the copies below, which are the whole of what makes this revision safe on a
logbook that already exists.

**The dive site: `nullif(location, '')`, not the column.** The new `name` is REQUIRED and
1-255, and a blank locality is reachable in the shipped app, whose update schema bounds that
field's length and not its emptiness. Migrated as-is it would become a place whose name is
the empty string §6.9 forbids: a nameless place on every read of that site, and a
`location.name` the format's own schema rejects on export. A blank is an absence spelled
wrongly, so it migrates to absence - and `DiveSiteLocationColumnsInput` is what stops a new
one arriving.

**The trip part: which column a label belongs in depends on what kind of label it is.**
`display_name` holds two populations. A place picked recently carries the app's own
composed `place, country`, which is exactly what the new `name` is for; one saved before
that keeps the provider's whole postal chain, which is `full_name`'s. Both lead with the
place, so a prefix test alone would file a chain under `name` and discard the readable
place beside it - and both of the corpus's genuinely divergent shapes are two parts, so a
part-count test alone would do the same. The label therefore moves into `name` only when
it leads with the place **and** is two comma-separated parts **and** fits the 255-wide
column; otherwise the place stays where it is and the label goes to `full_name`, which is
512 on both sides. The width test is a guard rather than an expected path - one row over
255 would abort `alembic upgrade head` with a 22001 and take the deploy with it - and
there is nowhere conforming to put such a label anyway, §6.9 capping `name` at 255. A blank
locality is an absence spelled wrongly on this table as well, and is read as one.

A part with a label and no name cannot be written through any schema this app has, but the
admin panel and raw SQL can reach it: the label becomes the name, truncated to the column,
and goes to `full_name` whole, so nothing is lost and no row arrives with a name the format
forbids.

**What the upgrade loses is one derivable segment, on one branch**: the bare place, which
is the leading part of the label that replaces it. Nothing is lost on the other branch, on
a typed place, or on a dive site.

**`downgrade()` reverses the moves, not just the DDL**, and is exactly reversing on the
dive-site half except for a blank the upgrade turned into an absence. On the trip half it
returns every label the upgrade put in `full_name` and cannot return the bare place the
first branch dropped - which is a loss the upgrade already took rather than a new one.

**Offline rendering.** Every data move is a single `UPDATE`, so `alembic upgrade head
--sql` renders them into the script and an operator applying the SQL by hand gets the
copies too.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9ec57e8a5182"
down_revision: str | None = "7c4be0a1d385"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_COPY_SITE_LOCALITY = sa.text("UPDATE dive_site SET location_name = nullif(location, '')")

_COPY_BACK_SITE_LOCALITY = sa.text("UPDATE dive_site SET location = location_name")

# `composed` is the three tests together: the label leads with the place, it is the app's
# own two-part form, and it fits the 255-wide column it would move into. A label that fails
# any of them keeps the place where it is, which is why a one-part label - a named bay the
# geocoder returned no address for - takes that branch too rather than overwriting the name
# with itself.
#
# `starts_with` rather than `LIKE name || '%'`: a place whose name contains `%` or `_` -
# "50% Wall" is not a name anyone would refuse - would otherwise match as a wildcard and
# take the wrong branch. `nullif` for the same reason the dive site's copy has one: a blank
# label is an absence spelled wrongly, and on a part with no name it would otherwise become
# a place called `''`.
_MOVE_PART_LABELS = sa.text(
    """
    UPDATE trip_part AS p
    SET name = CASE
                   WHEN p.name IS NULL THEN left(m.label, 255)
                   WHEN m.composed THEN m.label
                   ELSE p.name
               END,
        full_name = CASE WHEN m.composed THEN NULL ELSE m.label END
    FROM (
        SELECT id,
               nullif(display_name, '') AS label,
               (name IS NOT NULL
                AND starts_with(lower(display_name), lower(name))
                AND array_length(string_to_array(display_name, ','), 1) = 2
                AND length(display_name) <= 255) AS composed
        FROM trip_part
        WHERE nullif(display_name, '') IS NOT NULL
    ) AS m
    WHERE m.id = p.id
    """
)

# The label is whatever is in `full_name`, wherever the upgrade left one there. A row whose
# label went into `name` has nothing to give back: the bare place it replaced is gone.
_COPY_BACK_PART_LABELS = sa.text("UPDATE trip_part SET display_name = full_name")


def upgrade() -> None:
    op.add_column("dive_site", sa.Column("location_name", sa.String(length=255), nullable=True))
    op.add_column("dive_site", sa.Column("location_full_name", sa.String(length=512), nullable=True))
    op.add_column("dive_site", sa.Column("location_latitude", sa.Float(), nullable=True))
    op.add_column("dive_site", sa.Column("location_longitude", sa.Float(), nullable=True))
    op.add_column("dive_site", sa.Column("location_bbox_south", sa.Float(), nullable=True))
    op.add_column("dive_site", sa.Column("location_bbox_north", sa.Float(), nullable=True))
    op.add_column("dive_site", sa.Column("location_bbox_west", sa.Float(), nullable=True))
    op.add_column("dive_site", sa.Column("location_bbox_east", sa.Float(), nullable=True))

    # Copy, then drop, and the order is the whole of it: the drop below destroys what this
    # statement reads and no rollback puts it back.
    op.execute(_COPY_SITE_LOCALITY)

    # The index is a functional one over `lower()` and `coalesce()`, so it is dropped and
    # recreated rather than altered - and it has to go before the column it reads.
    op.drop_index("ux_dive_site_user_id_name_location_lower", table_name="dive_site")
    op.drop_column("dive_site", "location")
    op.create_index(
        "ux_dive_site_user_id_name_location_lower",
        "dive_site",
        ["user_id", sa.literal_column("lower(name)"), sa.literal_column("coalesce(lower(location_name), '')")],
        unique=True,
    )

    op.add_column("trip_part", sa.Column("full_name", sa.String(length=512), nullable=True))
    op.execute(_MOVE_PART_LABELS)
    op.drop_column("trip_part", "display_name")


def downgrade() -> None:
    op.add_column("trip_part", sa.Column("display_name", sa.String(length=512), nullable=True))
    op.execute(_COPY_BACK_PART_LABELS)
    op.drop_column("trip_part", "full_name")

    op.add_column("dive_site", sa.Column("location", sa.String(length=255), nullable=True))
    op.execute(_COPY_BACK_SITE_LOCALITY)

    op.drop_index("ux_dive_site_user_id_name_location_lower", table_name="dive_site")
    for column in (
        "location_bbox_east",
        "location_bbox_west",
        "location_bbox_north",
        "location_bbox_south",
        "location_longitude",
        "location_latitude",
        "location_full_name",
        "location_name",
    ):
        op.drop_column("dive_site", column)
    op.create_index(
        "ux_dive_site_user_id_name_location_lower",
        "dive_site",
        ["user_id", sa.literal_column("lower(name)"), sa.literal_column("coalesce(lower(location), '')")],
        unique=True,
    )
