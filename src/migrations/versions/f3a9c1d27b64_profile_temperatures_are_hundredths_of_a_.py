"""profile temperatures are hundredths of a degree

Revision ID: f3a9c1d27b64
Revises: 1538257d95db
Create Date: 2026-10-11 12:00:00.000000

DiveJSON's `temperature` channel moved from tenths to hundredths of a degree Celsius, and
`TEMPERATURE_SCALE` with it. This moves every stored profile, whatever its `parser_key`:

1. `dive_profile.min_temperature_c10` and `max_temperature_c10` become `_c100`. First, so the
   rename's lock holds off a write from the build before until this commits, after which that
   write fails on the old name rather than landing in tenths behind the rewrite.
2. Each temperature reading in `data` and both columns are multiplied by ten, and the row's
   `updated_at` set, which the web's profile URL keys on. A row with no temperature is
   untouched. No file is read: a file-backed row gains its real hundredths when the profile
   backfill re-reads it, which the pin bump in the same release puts every one of them behind.
   A value the multiplication would carry past a 32-bit integer is clamped to it; only an
   imported reading can be one, since the shaping bounded readings at the old scale.

`downgrade()` divides back, rounding half away from zero - exact on every row this wrote, and
the tenths reading of a row a backfill refined since - and renames the columns back.
"""

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "f3a9c1d27b64"
down_revision: str | None = "1538257d95db"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger(__name__)

FACTOR = 10
_INT32_MAX = 2**31 - 1
_INT32_MIN = -(2**31)
_BATCH = 200

TENTHS_COLUMNS = ("min_temperature_c10", "max_temperature_c10")
HUNDREDTHS_COLUMNS = ("min_temperature_c100", "max_temperature_c100")


def to_hundredths(value: int) -> int:
    return min(max(value * FACTOR, _INT32_MIN), _INT32_MAX)


def to_tenths(value: int) -> int:
    whole, remainder = divmod(abs(value), FACTOR)
    rounded = whole + (2 * remainder >= FACTOR)
    return -rounded if value < 0 else rounded


def rescaled(data: dict[str, Any], rescale: Callable[[int], int]) -> dict[str, Any]:
    """A stored payload with each temperature reading passed through `rescale`, and nothing else."""
    series = data.get("temperature")
    if not isinstance(series, dict) or not isinstance(series.get("v"), list):
        return data
    values = [rescale(v) if isinstance(v, int) and not isinstance(v, bool) else v for v in series["v"]]
    return {**data, "temperature": {**series, "v": values}}


def _rewrite(connection: sa.Connection, columns: tuple[str, str], rescale: Callable[[int], int]) -> int:
    """Step 2 in either direction, on `columns` as they are named by then. Returns the rows written."""
    low, high = columns
    ids = list(
        connection.execute(
            sa.text(
                f"SELECT id FROM dive_profile WHERE data ? 'temperature' OR {low} IS NOT NULL OR {high} IS NOT NULL "  # noqa: S608 - the column names are the literals above
                "ORDER BY id"
            )
        ).scalars()
    )
    select = (
        sa.text(f"SELECT id, data, {low} AS low, {high} AS high FROM dive_profile WHERE id IN :ids")  # noqa: S608
        .bindparams(sa.bindparam("ids", expanding=True))
        .columns(id=sa.Integer, data=JSONB, low=sa.Integer, high=sa.Integer)
    )
    update = sa.text(
        f"UPDATE dive_profile SET data = :data, {low} = :low, {high} = :high, updated_at = :now WHERE id = :id"  # noqa: S608
    ).bindparams(sa.bindparam("data", type_=JSONB))
    now = datetime.now(UTC)
    for start in range(0, len(ids), _BATCH):
        rows = connection.execute(select, {"ids": ids[start : start + _BATCH]}).all()
        connection.execute(
            update,
            [
                {
                    "id": row.id,
                    "data": rescaled(row.data, rescale),
                    "low": None if row.low is None else rescale(row.low),
                    "high": None if row.high is None else rescale(row.high),
                    "now": now,
                }
                for row in rows
            ],
        )
    return len(ids)


def _rename(old: tuple[str, str], new: tuple[str, str]) -> None:
    for before, after in zip(old, new, strict=True):
        op.alter_column("dive_profile", before, new_column_name=after)


def upgrade() -> None:
    _rename(TENTHS_COLUMNS, HUNDREDTHS_COLUMNS)
    if context.is_offline_mode():
        return
    clamped = 0

    def counted(value: int) -> int:
        nonlocal clamped
        moved = to_hundredths(value)
        clamped += moved != value * FACTOR
        return moved

    _rewrite(op.get_bind(), HUNDREDTHS_COLUMNS, counted)
    if clamped:
        logger.warning("%d stored temperature value(s) were past what hundredths can hold; clamped", clamped)


def downgrade() -> None:
    _rename(HUNDREDTHS_COLUMNS, TENTHS_COLUMNS)
    if not context.is_offline_mode():
        _rewrite(op.get_bind(), TENTHS_COLUMNS, to_tenths)
