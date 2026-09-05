"""per-user dive form presets and a hidden-fields preference

A diver may hide dive-form fields they never fill in, and save named sets of that choice.
Two objects: `dive_form_preset`, a named hidden set per account, and
`user.dive_form_hidden_fields`, the account's *current* one - applying a preset copies the
first into the second, so a one-off toggle never edits a preset.

**The backfill is what makes this more than DDL.** Every account is seeded with three
default presets at registration, and the accounts that existed before this revision never
went through that path - so the revision does for them what `POST /auth/complete` does for
everyone after it. Without it a self-hoster's own account is the one account on the instance
whose panel offers nothing to apply. Autogenerate drafted the DDL and does not see a data
step at all; the shape of this one follows `c3c2c4dd4c27`:

- **It must survive offline rendering.** `tests/test_migrations.py` runs
  `alembic upgrade head --sql` against no database, so the backfill is guarded with
  `context.is_offline_mode()` and skipped there. The rendered DDL stays complete.
- **At zero accounts it does nothing**, which is a fresh install and CI's bare runner.
- **The three default sets are a frozen copy, not an import** of
  `app.services.dive_form_presets`. A revision is history: if a later change renamed a
  `DiveFormField` member or dropped a default, an import here would either rewrite what this
  migration did in the past or - worse - raise `ImportError` at revision-load time and take
  every `alembic upgrade head` with it, on a fresh install as much as an existing one. What
  the live definition owes this copy is nothing; what this copy owes the accounts it seeded
  is that they got the defaults as they stood on the day they were seeded.

The insert skips a name the account already holds, compared case-insensitively, which is the
same "add what is missing, never overwrite" rule the restore endpoint keeps. At upgrade time
nothing can match - the table is created three statements above - so the guard is doing no
work here; it is what makes `_backfill_default_presets` a callable a test can drive twice,
and what would keep a hand re-run from tripping the unique index.

Revision ID: e0cfbd603859
Revises: d3b1700eb489
Create Date: 2026-09-05 13:43:20.445461

"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.engine import Connection
from uuid6 import uuid7

# revision identifiers, used by Alembic.
revision: str = "e0cfbd603859"
down_revision: str | None = "d3b1700eb489"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy of `app.services.dive_form_presets.DEFAULT_PRESETS` as it stood when this
# revision was written - see the module docstring for why it is copied rather than imported.
# The inner strings are `DiveFormField` values, and they are data: the column stores them.
_DEFAULT_PRESETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Basic",
        (
            "course_uuid",
            "avg_depth",
            "visibility",
            "water_type",
            "altitude",
            "mixtures",
            "gear_item_uuids",
            "weight",
            "species_uuids",
            "mixture.po2_limit",
            "mixture.start_pressure",
            "mixture.end_pressure",
            "mixture.role",
            "mixture.usage",
        ),
    ),
    ("Recreational", ("altitude", "mixture.po2_limit", "mixture.role", "mixture.usage")),
    ("Technical", ()),
)

_ACCOUNTS_MISSING = sa.text(
    """
    SELECT u.id
    FROM "user" AS u
    WHERE NOT EXISTS (
        SELECT 1 FROM dive_form_preset AS p
        WHERE p.user_id = u.id AND lower(p.name) = lower(:name)
    )
    ORDER BY u.id
    """
)

# The two typed binds are not decoration. A `sa.text()` statement carries no column types,
# so without them the driver is handed a bare Python list for a JSON column and a bare
# `UUID` for a `uuid` one, and what happens next is the driver's business rather than
# SQLAlchemy's. Naming the types is what serializes the list and adapts the uuid.
_INSERT_PRESET = sa.text(
    """
    INSERT INTO dive_form_preset (user_id, name, hidden_fields, uuid, created_at)
    VALUES (:user_id, :name, :hidden_fields, :uuid, :created_at)
    """
).bindparams(
    sa.bindparam("hidden_fields", type_=sa.JSON()),
    sa.bindparam("uuid", type_=sa.Uuid()),
    sa.bindparam("created_at", type_=sa.DateTime(timezone=True)),
)


def _backfill_default_presets(connection: Connection) -> int:
    """Give every account the default presets it is missing, and answer how many rows that
    was.

    Soft-deleted accounts are seeded too. They are pending purge and the cascade will take
    these rows with them, so the cost is nothing - and `POST /auth/restore` can bring one
    back, at which point being the one account on the instance with no presets would be a
    puzzle with no cause visible to the diver.

    A uuid per row from `uuid7`, matching every other row in this schema: `PublicUUIDMixin`'s
    `default_factory` is applied by the ORM on construction and nothing here constructs a
    model, so a bare INSERT would otherwise hand Postgres a NULL for a NOT NULL column.
    `gen_random_uuid()` would have avoided the row loop and is deliberately not used - it is
    uuid4, and these are the only rows in the database that would not be time-ordered.
    """
    created_at = datetime.now(UTC)
    written = 0

    for name, hidden_fields in _DEFAULT_PRESETS:
        user_ids = connection.execute(_ACCOUNTS_MISSING, {"name": name}).scalars().all()
        if not user_ids:
            continue

        connection.execute(
            _INSERT_PRESET,
            [
                {
                    "user_id": user_id,
                    "name": name,
                    "hidden_fields": list(hidden_fields),
                    "uuid": uuid7(),
                    "created_at": created_at,
                }
                for user_id in user_ids
            ],
        )
        written += len(user_ids)

    return written


def upgrade() -> None:
    op.create_table(
        "dive_form_preset",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("hidden_fields", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("uuid", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_dive_form_preset_user_id"), "dive_form_preset", ["user_id"], unique=False)
    op.create_index("ix_dive_form_preset_user_id_name", "dive_form_preset", ["user_id", "name"], unique=False)
    op.create_index(op.f("ix_dive_form_preset_uuid"), "dive_form_preset", ["uuid"], unique=True)
    op.create_index(
        "ux_dive_form_preset_user_id_name_lower",
        "dive_form_preset",
        ["user_id", sa.literal_column("lower(name)")],
        unique=True,
    )
    op.add_column("user", sa.Column("dive_form_hidden_fields", sa.JSON(), server_default="[]", nullable=False))

    # Skipped when rendering SQL with no database: there is nothing to read and nowhere to
    # write it. Standard Alembic practice for a data migration, and what keeps
    # `tests/test_migrations.py`'s offline render working.
    if not context.is_offline_mode():
        _backfill_default_presets(op.get_bind())


def downgrade() -> None:
    op.drop_column("user", "dive_form_hidden_fields")
    op.drop_index("ux_dive_form_preset_user_id_name_lower", table_name="dive_form_preset")
    op.drop_index(op.f("ix_dive_form_preset_uuid"), table_name="dive_form_preset")
    op.drop_index("ix_dive_form_preset_user_id_name", table_name="dive_form_preset")
    op.drop_index(op.f("ix_dive_form_preset_user_id"), table_name="dive_form_preset")
    op.drop_table("dive_form_preset")
