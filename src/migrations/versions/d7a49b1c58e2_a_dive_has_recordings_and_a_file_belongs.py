"""a dive has recordings, and a file belongs to one of them

Revision ID: d7a49b1c58e2
Revises: e0cfbd603859
Create Date: 2026-09-10 21:40:00.000000

`dive_recording` arrives between `dive` and its files and profiles. A dive used to have at
most one of each, keyed on `dive_id` by a unique index; it now has an ordered list of
recordings, each of which is one device's record and holds its own files and its own
profile.

**The data move is hand-written and autogenerate never saw it.** Autogenerate drafted the
table, the two columns and the four index changes; it does not see a backfill at all, and
the backfill here is the whole of what makes the revision safe. Local data is disposable
(there is no deployment but the developer's), but this revision is what a self-hoster runs
on an instance holding their entire logbook, so it moves rows rather than dropping them.

**One recording per dive that has anything to record.** Every dive holding a `dive_file`
or a `dive_profile` gets exactly one - one, not two, for a dive holding both - and a dive
holding neither gets none, because a hand-entered dive has nothing a device recorded.

**A migrated recording inherits the dive's figures**, which is the one exception to "no
profile, no gate figures" and a deliberate one. `start_time`, `utc_offset_minutes`,
`duration` and `max_depth` are copied from `dive` because the dive being migrated had
exactly one recording - so those numbers *are* that recording's, not an approximation of
them. The device columns stay NULL: nothing in the old schema recorded what wrote a file,
and `backfill_tech_fields` re-parses every stored file already, so one run fills them for
every file-backed recording on an existing instance. A recording that logbook import
created has no file to re-parse and came from a document that carried no device, so it
stays device-less until a file of the same recording arrives and the fill rule supplies
them.

**Offline rendering.** `tests/test_migrations.py` runs `alembic upgrade head --sql` against
no database, so the backfill is guarded with `context.is_offline_mode()` and skipped there.
The rendered DDL stays complete; what is missing offline is a data move, which is what
`--sql` cannot express in any revision.

**Ordering inside `upgrade`.** The table and the nullable columns come first, then the
backfill, then the `NOT NULL` and the index changes - because `recording_id` cannot be
`NOT NULL` while the rows that will fill it do not exist yet, and `ux_dive_profile_dive_id`
must not be dropped before its replacement is created against a populated column. The whole
revision is one transaction (Postgres DDL is transactional), so a failure anywhere rolls
the lot back.

`downgrade` raises. Folding several recordings back into one file per dive would have to
choose which of them to keep and destroy the rest, which is not a migration but a data
loss with a `downgrade` label on it.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import context, op
from uuid6 import uuid7

# revision identifiers, used by Alembic.
revision: str = "d7a49b1c58e2"
down_revision: str | None = "e0cfbd603859"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The dives that need a recording, and the figures it inherits. Driven off `dive` rather
# than off the two child tables, so that a dive carrying both a file and a profile - the
# ordinary case - yields one recording rather than a pair that would then have to be
# reconciled.
_DIVES_TO_MIGRATE = sa.text(
    """
    SELECT d.id AS dive_id,
           d.user_id,
           d.start_time,
           d.utc_offset_minutes,
           d.duration,
           d.max_depth
    FROM dive AS d
    WHERE EXISTS (SELECT 1 FROM dive_file AS f WHERE f.dive_id = d.id)
       OR EXISTS (SELECT 1 FROM dive_profile AS p WHERE p.dive_id = d.id)
    ORDER BY d.id
    """
)

_INSERT_RECORDING = sa.text(
    """
    INSERT INTO dive_recording (
        uuid, created_at, dive_id, user_id, ordinal,
        start_time, utc_offset_minutes, duration, max_depth
    )
    VALUES (
        :uuid, :created_at, :dive_id, :user_id, 0,
        :start_time, :utc_offset_minutes, :duration, :max_depth
    )
    """
)


def _backfill() -> int:
    """One recording per dive that has a file or a profile, then the links. Returns the count.

    **No `is_deleted` filter**, unlike almost every other query in this app, and leaving it
    out is deliberate. The two `recording_id` columns become `NOT NULL` immediately after
    this runs, so a file or profile this pass did not reach fails that `ALTER` and takes the
    whole upgrade with it. Whether the dive above it is visible to its owner has no bearing
    on whether its child rows need a parent.

    A uuid per row from `uuid7`, matching every other row in this schema and following
    revision `e0cfbd603859`'s reasoning exactly: `gen_random_uuid()` would avoid the loop
    and is uuid4, which would make these the only public identifiers in the database that
    are not time-ordered.

    `ordinal` is 0 throughout, because every dive being migrated had at most one file and at
    most one profile - which is exactly the invariant the two unique indexes dropped below
    were enforcing.
    """
    connection = op.get_bind()
    created_at = datetime.now(UTC)

    rows = connection.execute(_DIVES_TO_MIGRATE).mappings().all()
    if not rows:
        return 0

    connection.execute(
        _INSERT_RECORDING,
        [{"uuid": uuid7(), "created_at": created_at, **dict(row)} for row in rows],
    )

    # Point each file and each profile at the recording of its dive. Unambiguous because
    # exactly one recording per dive exists at this point.
    for table in ("dive_file", "dive_profile"):
        connection.execute(
            sa.text(
                f"""
                UPDATE {table} AS t
                SET recording_id = r.id
                FROM dive_recording AS r
                WHERE r.dive_id = t.dive_id
                """  # noqa: S608 - `table` is one of two literals above, never input
            )
        )
    return len(rows)


def upgrade() -> None:
    op.create_table(
        "dive_recording",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dive_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("device_brand", sa.String(length=64), nullable=True),
        sa.Column("device_model", sa.String(length=64), nullable=True),
        sa.Column("device_serial", sa.String(length=64), nullable=True),
        sa.Column("device_firmware", sa.String(length=32), nullable=True),
        sa.Column("device_name", sa.String(length=64), nullable=True),
        sa.Column("device_dive_number", sa.Integer(), nullable=True),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("utc_offset_minutes", sa.Integer(), nullable=True),
        sa.Column("duration", sa.Integer(), nullable=True),
        sa.Column("max_depth", sa.Float(), nullable=True),
        sa.Column("uuid", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["dive_id"], ["dive.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_dive_recording_dive_id"), "dive_recording", ["dive_id"], unique=False)
    op.create_index(op.f("ix_dive_recording_uuid"), "dive_recording", ["uuid"], unique=True)
    op.create_index("ix_dive_recording_user_id_start_time", "dive_recording", ["user_id", "start_time"], unique=False)
    op.create_index("ux_dive_recording_dive_id_ordinal", "dive_recording", ["dive_id", "ordinal"], unique=True)

    # Nullable for the length of the backfill and `NOT NULL` after it - the rows that will
    # fill the column do not exist until `_backfill` has run.
    op.add_column("dive_file", sa.Column("recording_id", sa.Integer(), nullable=True))
    op.add_column("dive_profile", sa.Column("recording_id", sa.Integer(), nullable=True))

    if not context.is_offline_mode():
        _backfill()

    op.alter_column("dive_file", "recording_id", existing_type=sa.Integer(), nullable=False)
    op.alter_column("dive_profile", "recording_id", existing_type=sa.Integer(), nullable=False)
    op.create_foreign_key(
        "fk_dive_file_recording_id_dive_recording",
        "dive_file",
        "dive_recording",
        ["recording_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_dive_profile_recording_id_dive_recording",
        "dive_profile",
        "dive_recording",
        ["recording_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(op.f("ix_dive_file_recording_id"), "dive_file", ["recording_id"], unique=False)

    # The two "one per dive" slots go, and `dive_id` keeps a plain index in each table:
    # both are still read by dive (`erase_dive`, the export loader, the archive writer),
    # and the unique index was what served that read before.
    op.drop_index("ux_dive_file_dive_id", table_name="dive_file")
    op.create_index(op.f("ix_dive_file_dive_id"), "dive_file", ["dive_id"], unique=False)
    op.drop_index("ux_dive_profile_dive_id", table_name="dive_profile")
    op.create_index(op.f("ix_dive_profile_dive_id"), "dive_profile", ["dive_id"], unique=False)
    op.create_index("ux_dive_profile_recording_id", "dive_profile", ["recording_id"], unique=True)


def downgrade() -> None:
    raise NotImplementedError(
        "Recordings cannot be folded back into one file per dive without choosing which of a dive's "
        "recordings to keep and destroying the rest."
    )
