"""the profile axis is milliseconds, and a recording carries its readouts and its salinity

Revision ID: ce09bc7d4c64
Revises: dd420c8df9de
Create Date: 2026-09-25 09:21:42.000000

DiveJSON moved a computer's readouts - `cns_start`, `cns_end`, `otu_start`, `otu_end`,
`surface_pressure` - and its `en13319` setting from the dive onto the recording, and put the
profile axis in milliseconds. This moves the stored data with it, in order:

1. `dive_recording` gains the five readout columns and `salinity`, with the `CHECK`s `dive`
   carried re-created under the recording's name.
2. Each dive's readouts are copied onto its ordinal-0 recording. A dive that carries a readout
   and has no recording gets one at ordinal 0 carrying the readouts and the dive's start and
   nothing else - a recording of readouts alone, which the format admits.
3. `water_type = 'en13319'` becomes the ordinal-0 recording's `salinity`, and the dive's
   `water_type` NULL. A dive with neither a recording nor a readout has nowhere for it to go,
   so the value is dropped and the count logged.
4. `dive` loses the five columns and their `CHECK`s.
5. Every `dive_profile` row's axis - each series' `t`, each pressure series' `t`, each
   event's `t` and `duration` - is multiplied by a thousand and stamped with extractor
   version 5. No file is read, so a stored profile keeps its whole-second resolution and its
   first-reading origin; `src/scripts/backfill_dive_profiles.py --force` re-derives the
   file-backed ones.

`downgrade()` reverses the data as well as the DDL: the readouts go back onto the dive from the
primary recording (every other recording's are lost), `en13319` comes back where a primary
recording carries it and the dive's `water_type` is NULL, the recordings step 2 could have
inserted - no device, setting, file or profile - are deleted and the ordinals closed up, and the
axis is divided, refusing when a division would not be exact.
"""

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import JSONB
from uuid6 import uuid7

# revision identifiers, used by Alembic.
revision: str = "ce09bc7d4c64"
down_revision: str | None = "dd420c8df9de"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger(__name__)

READOUT_COLUMNS: tuple[str, ...] = ("cns_start", "cns_end", "otu_start", "otu_end", "surface_pressure_bar")

# Each readout's `CHECK`, keyed by column. The same predicate on both tables; only the name moves.
_CHECKS: dict[str, str] = {
    "cns_start": "cns_start IS NULL OR cns_start >= 0",
    "cns_end": "cns_end IS NULL OR cns_end >= 0",
    "otu_start": "otu_start IS NULL OR otu_start >= 0",
    "otu_end": "otu_end IS NULL OR otu_end >= 0",
    "surface_pressure_bar": (
        "surface_pressure_bar IS NULL OR (surface_pressure_bar >= 0.4 AND surface_pressure_bar <= 1.2)"
    ),
}
_CHECK_SUFFIX: dict[str, str] = {
    "cns_start": "cns_start_non_negative",
    "cns_end": "cns_end_non_negative",
    "otu_start": "otu_start_non_negative",
    "otu_end": "otu_end_non_negative",
    "surface_pressure_bar": "surface_pressure_range",
}

EXTRACTOR_VERSION_BEFORE = 4
EXTRACTOR_VERSION_AFTER = 5
MILLISECONDS_PER_SECOND = 1000
_INT32_MAX = 2**31 - 1
_PROFILE_BATCH = 200

_ANY_READOUT = " OR ".join(f"d.{column} IS NOT NULL" for column in READOUT_COLUMNS)

# Step 2, the copy. Deleted dives included: nothing else carries their readouts once the dive
# columns go, and an import can restore a deleted dive.
COPY_READOUTS = f"""
    UPDATE dive_recording AS r
       SET {", ".join(f"{column} = d.{column}" for column in READOUT_COLUMNS)}
      FROM dive AS d
     WHERE r.dive_id = d.id
       AND r.ordinal = 0
       AND ({_ANY_READOUT})
"""  # noqa: S608 - the column names are the literals above

DIVES_WITH_READOUTS_AND_NO_RECORDING = f"""
    SELECT d.id AS dive_id, d.user_id, d.start_time, d.utc_offset_minutes,
           {", ".join(f"d.{column}" for column in READOUT_COLUMNS)}
      FROM dive AS d
     WHERE ({_ANY_READOUT})
       AND NOT EXISTS (SELECT 1 FROM dive_recording AS r WHERE r.dive_id = d.id)
     ORDER BY d.id
"""  # noqa: S608 - the column names are the literals above

INSERT_READOUT_RECORDING = f"""
    INSERT INTO dive_recording (
        uuid, created_at, dive_id, user_id, ordinal, start_time, utc_offset_minutes,
        {", ".join(READOUT_COLUMNS)}
    )
    VALUES (
        :uuid, :created_at, :dive_id, :user_id, 0, :start_time, :utc_offset_minutes,
        {", ".join(f":{column}" for column in READOUT_COLUMNS)}
    )
"""  # noqa: S608 - the column names are the literals above

# Step 3, a named-literal repair and not a vocabulary sweep.
MOVE_EN13319 = """
    UPDATE dive_recording AS r
       SET salinity = 'en13319'
      FROM dive AS d
     WHERE r.dive_id = d.id
       AND r.ordinal = 0
       AND d.water_type = 'en13319'
"""
COUNT_EN13319_DROPPED = """
    SELECT count(*)
      FROM dive AS d
     WHERE d.water_type = 'en13319'
       AND NOT EXISTS (SELECT 1 FROM dive_recording AS r WHERE r.dive_id = d.id)
"""
CLEAR_EN13319 = "UPDATE dive SET water_type = NULL WHERE water_type = 'en13319'"

# The reverse of each.
RESTORE_READOUTS = f"""
    UPDATE dive AS d
       SET {", ".join(f"{column} = r.{column}" for column in READOUT_COLUMNS)}
      FROM dive_recording AS r
     WHERE r.dive_id = d.id
       AND r.ordinal = 0
"""  # noqa: S608 - the column names are the literals above
RESTORE_EN13319 = """
    UPDATE dive AS d
       SET water_type = 'en13319'
      FROM dive_recording AS r
     WHERE r.dive_id = d.id
       AND r.ordinal = 0
       AND r.salinity = 'en13319'
       AND d.water_type IS NULL
"""
# What step 2 inserts, and what a recording without the dive's readouts can no longer say: no
# device, no setting, no file, no profile.
DELETE_READOUT_RECORDINGS = """
    DELETE FROM dive_recording AS r
     WHERE r.device_brand IS NULL AND r.device_model IS NULL AND r.device_serial IS NULL
       AND r.device_firmware IS NULL AND r.device_name IS NULL AND r.device_dive_number IS NULL
       AND r.mode IS NULL
       AND r.deco_algorithm IS NULL AND r.deco_name IS NULL AND r.deco_gf_low IS NULL
       AND r.deco_gf_high IS NULL AND r.deco_conservatism IS NULL
       AND NOT EXISTS (SELECT 1 FROM dive_file AS f WHERE f.recording_id = r.id)
       AND NOT EXISTS (SELECT 1 FROM dive_profile AS p WHERE p.recording_id = r.id)
"""
# Closes any gap the delete left, through a negative slot: `ux_dive_recording_dive_id_ordinal`
# is checked per row, so moving 1 onto a 0 that is itself about to move would collide.
RENUMBER_ORDINALS: tuple[str, ...] = (
    "UPDATE dive_recording SET ordinal = -ordinal - 1",
    """
    UPDATE dive_recording AS r
       SET ordinal = ranked.position
      FROM (
          SELECT id, row_number() OVER (PARTITION BY dive_id ORDER BY ordinal DESC) - 1 AS position
            FROM dive_recording
      ) AS ranked
     WHERE r.id = ranked.id
    """,
)


def rescaled(data: dict[str, Any], rescale: Callable[[int], int]) -> dict[str, Any]:
    """A stored profile payload with every axis entry passed through `rescale`.

    Walks the payload's own shape rather than a list of channel names, so a key this revision
    predates keeps its values: a single series `{"t", "v"}`, the `pressure` list of them, and
    the `events` list, each with its own `t`.
    """
    moved: dict[str, Any] = {}
    for key, value in data.items():
        if key in ("pressure", "events") and isinstance(value, list):
            moved[key] = [_rescaled_entry(entry, rescale) for entry in value]
        elif isinstance(value, dict) and "t" in value:
            moved[key] = _rescaled_entry(value, rescale)
        else:
            moved[key] = value
    return moved


def _rescaled_entry(entry: dict[str, Any], rescale: Callable[[int], int]) -> dict[str, Any]:
    times = entry.get("t")
    if isinstance(times, list):
        return {**entry, "t": [rescale(t) for t in times]}
    if isinstance(times, int):
        return {**entry, "t": rescale(times)}
    return entry


def _sample_span(data: dict[str, Any]) -> int:
    """The latest sample time in a payload, events aside - what `duration` covers at least."""
    series: list[Any] = []
    for key, value in data.items():
        if key == "pressure" and isinstance(value, list):
            series.extend(value)
        elif key != "events":
            series.append(value)
    times = [t for entry in series if isinstance(entry, dict) for t in entry.get("t") or [] if isinstance(t, int)]
    return max(times, default=0)


def _to_milliseconds(value: int) -> int:
    return value * MILLISECONDS_PER_SECOND


def _to_seconds(value: int) -> int:
    if value % MILLISECONDS_PER_SECOND:
        raise RuntimeError(
            f"A stored profile carries {value} ms, which is not a whole second, so its axis cannot go back to "
            "seconds without losing it. This downgrade refuses rather than round."
        )
    return value // MILLISECONDS_PER_SECOND


_SELECT_PROFILES = (
    sa.text("SELECT id, duration, data FROM dive_profile WHERE id IN :ids")
    .bindparams(sa.bindparam("ids", expanding=True))
    .columns(id=sa.Integer, duration=sa.Integer, data=JSONB)
)
_UPDATE_PROFILE = sa.text(
    "UPDATE dive_profile SET duration = :duration, data = :data, extractor_version = :version, updated_at = :now "
    "WHERE id = :id"
).bindparams(sa.bindparam("data", type_=JSONB))


def _profile_ids(connection: sa.Connection) -> list[int]:
    return list(connection.execute(sa.text("SELECT id FROM dive_profile ORDER BY id")).scalars())


def _rewrite_profiles(connection: sa.Connection, *, rescale: Callable[[int], int], version: int) -> int:
    """Step 5 in either direction, a batch of rows at a time. Returns how many rows it wrote.

    In Python rather than one `jsonb` statement: the payload is nested three ways and the
    downgrade has to refuse a remainder, which reads better here than in SQL.
    """
    ids = _profile_ids(connection)
    now = datetime.now(UTC)
    clamped = 0
    for start in range(0, len(ids), _PROFILE_BATCH):
        batch = ids[start : start + _PROFILE_BATCH]
        updates = []
        for row in connection.execute(_SELECT_PROFILES, {"ids": batch}).all():
            moved = rescaled(row.data, rescale)
            duration = rescale(row.duration)
            if duration > _INT32_MAX:
                # Only a document's declared span can get here - an imported profile claiming
                # more than twenty-four days. The samples' own span stands in, as the importer
                # does for a span it cannot store.
                duration = min(_sample_span(moved), _INT32_MAX)
                clamped += 1
            updates.append({"id": row.id, "duration": duration, "data": moved, "version": version, "now": now})
        if updates:
            connection.execute(_UPDATE_PROFILE, updates)
    if clamped:
        logger.warning(
            "%d stored profile(s) declared a span past what milliseconds can hold; kept their samples'", clamped
        )
    return len(ids)


def _insert_readout_recordings(connection: sa.Connection) -> int:
    rows = connection.execute(sa.text(DIVES_WITH_READOUTS_AND_NO_RECORDING)).mappings().all()
    if rows:
        created_at = datetime.now(UTC)
        connection.execute(
            sa.text(INSERT_READOUT_RECORDING),
            [{"uuid": uuid7(), "created_at": created_at, **dict(row)} for row in rows],
        )
    return len(rows)


def _move_data() -> None:
    connection = op.get_bind()
    connection.execute(sa.text(COPY_READOUTS))
    _insert_readout_recordings(connection)
    connection.execute(sa.text(MOVE_EN13319))
    dropped = connection.execute(sa.text(COUNT_EN13319_DROPPED)).scalar_one()
    if dropped:
        logger.warning("Dropped water_type 'en13319' from %d dive(s) with no recording to carry it", dropped)
    connection.execute(sa.text(CLEAR_EN13319))


def _move_data_back() -> None:
    connection = op.get_bind()
    connection.execute(sa.text(RESTORE_READOUTS))
    connection.execute(sa.text(RESTORE_EN13319))
    connection.execute(sa.text(DELETE_READOUT_RECORDINGS))
    for statement in RENUMBER_ORDINALS:
        connection.execute(sa.text(statement))


def upgrade() -> None:
    op.add_column("dive_recording", sa.Column("salinity", sa.String(length=32), nullable=True))
    for column in READOUT_COLUMNS:
        op.add_column("dive_recording", sa.Column(column, sa.Float(), nullable=True))
        op.create_check_constraint(f"ck_dive_recording_{_CHECK_SUFFIX[column]}", "dive_recording", _CHECKS[column])

    if not context.is_offline_mode():
        _move_data()

    for column in READOUT_COLUMNS:
        op.drop_constraint(f"ck_dive_{_CHECK_SUFFIX[column]}", "dive", type_="check")
        op.drop_column("dive", column)

    if not context.is_offline_mode():
        _rewrite_profiles(op.get_bind(), rescale=_to_milliseconds, version=EXTRACTOR_VERSION_AFTER)


def downgrade() -> None:
    if not context.is_offline_mode():
        _rewrite_profiles(op.get_bind(), rescale=_to_seconds, version=EXTRACTOR_VERSION_BEFORE)

    for column in READOUT_COLUMNS:
        op.add_column("dive", sa.Column(column, sa.Float(), nullable=True))
        op.create_check_constraint(f"ck_dive_{_CHECK_SUFFIX[column]}", "dive", _CHECKS[column])

    if not context.is_offline_mode():
        _move_data_back()

    for column in READOUT_COLUMNS:
        op.drop_constraint(f"ck_dive_recording_{_CHECK_SUFFIX[column]}", "dive_recording", type_="check")
        op.drop_column("dive_recording", column)
    op.drop_column("dive_recording", "salinity")
