"""an imported dive takes its bottom temperature from its profile

Revision ID: 53ea55b922e5
Revises: cf3c73ed02f4
Create Date: 2026-10-01 15:28:05.352815

Logbook import now applies the dive form's default for a bottom temperature its document does
not state - the coldest reading of the primary recording's temperature channel
(`dive_reader.bottom_temperature`). This gives the dives already stored without one the same
value, from the stored profile's `min_temperature_c10`, which the capped samples keep exactly.

It cannot tell a temperature never filled from one a diver cleared, and fills both.

`downgrade()` is a no-op: nothing records which rows this filled.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "53ea55b922e5"
down_revision: str | None = "cf3c73ed02f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Module-level so `tests/test_bottom_temperature_backfill.py` can run it against a live Postgres.
# Float division, as the app divides, so both land on the same double.
BACKFILL = """
    UPDATE dive AS d
       SET bottom_temperature = p.min_temperature_c10::double precision / 10
      FROM dive_recording AS r
      JOIN dive_profile AS p ON p.recording_id = r.id
     WHERE r.dive_id = d.id
       AND r.ordinal = 0
       AND d.bottom_temperature IS NULL
       AND p.min_temperature_c10 IS NOT NULL
"""


def upgrade() -> None:
    op.execute(BACKFILL)


def downgrade() -> None:
    """Nothing to undo - see the module docstring."""
