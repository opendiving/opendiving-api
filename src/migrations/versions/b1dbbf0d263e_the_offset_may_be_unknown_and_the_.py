"""the offset may be unknown, and the surface-pressure floor follows the spec

Revision ID: b1dbbf0d263e
Revises: c4d81e6b3f57
Create Date: 2026-09-04 12:48:30.181559

Two changes to `dive`, both of them what logbook import needs the table to admit.

**`utc_offset_minutes` becomes nullable.** NULL is a third state rather than a missing
value: the wall clock was recorded and the instant is unknown, which is DiveJSON's local
date-time (spec §5.2). It exists because real migration sources - UDDF pipelines above all
- destroy offsets, and a converter meeting one has no honest third option: fabricating an
offset poisons the record undetectably and dropping the dive is the data loss the format
exists to end. `start_time` then holds that wall clock labelled UTC, because a
`timestamptz` has nowhere else to put it.

**The server default stays `0`, deliberately.** Every existing row keeps its offset and
nothing about them changes; the default is what stops a `Dive(...)` built without one
(tests, the admin panel) from silently claiming the unknown state, which would be a claim
about the data rather than a missing keyword argument. Only the importer ever writes NULL,
and it writes it explicitly.

**`ck_dive_surface_pressure_range`'s floor moves from 0.5 to 0.4 bar.** Ambient pressure at
this table's own `ck_dive_altitude_range` ceiling of 6500 m is about 0.44 bar, so the old
floor refused readings the altitude bound blesses - and 0.4 is the DiveJSON floor (spec
§6.2), which is where the contradiction was noticed. Autogenerate does not see a
constraint whose *text* changed under an unchanged name, so this half is written by hand.

The band only widens, so no existing row can violate the new constraint and there is
nothing to repair before adding it - unlike revision `c4d81e6b3f57`, which had to clear
violating values first.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1dbbf0d263e"
down_revision: str | None = "c4d81e6b3f57"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_dive_surface_pressure_range"


def upgrade() -> None:
    op.alter_column(
        "dive",
        "utc_offset_minutes",
        existing_type=sa.INTEGER(),
        nullable=True,
        existing_server_default=sa.text("0"),
    )
    op.drop_constraint(_CONSTRAINT, "dive", type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        "dive",
        "surface_pressure_bar IS NULL OR (surface_pressure_bar >= 0.4 AND surface_pressure_bar <= 1.2)",
    )


def downgrade() -> None:
    # The narrower band first: a row between 0.4 and 0.5 bar could only have arrived through
    # an import, and it has to go before the old constraint can be added back.
    op.execute(
        "UPDATE dive SET surface_pressure_bar = NULL "
        "WHERE surface_pressure_bar IS NOT NULL AND surface_pressure_bar < 0.5"
    )
    op.drop_constraint(_CONSTRAINT, "dive", type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        "dive",
        "surface_pressure_bar IS NULL OR (surface_pressure_bar >= 0.5 AND surface_pressure_bar <= 1.2)",
    )
    # Likewise: an offset-unknown dive has no offset to restore, and the column cannot go
    # back to NOT NULL while one exists. `0` is what the column's own default has always
    # meant for a row nobody gave an offset - and it is a lossy answer, which is the honest
    # shape of undoing a change that admitted a state the old schema could not express.
    op.execute("UPDATE dive SET utc_offset_minutes = 0 WHERE utc_offset_minutes IS NULL")
    op.alter_column(
        "dive",
        "utc_offset_minutes",
        existing_type=sa.INTEGER(),
        nullable=False,
        existing_server_default=sa.text("0"),
    )
