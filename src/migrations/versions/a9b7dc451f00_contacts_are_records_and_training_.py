"""contacts are records, and training centers become them

Revision ID: a9b7dc451f00
Revises: ab9a5add4fee
Create Date: 2026-09-25 20:00:49.939672

`contact` is a party a diver deals with - a dive center, a school, a shop, a place they
stayed - referenced from a dive, a course, a certification, a gear service record and a trip
part. The two `training_center` strings on `course` and `certification` become rows of it and
go. Autogenerate drafted the DDL; the three data steps are hand-written and it sees none of
them.

**The backfill reads live rows only.** One contact per distinct trimmed, lowercased string
per diver, across every course and every certification that is not soft-deleted - named with
the first spelling seen, trimmed (courses before cards, lowest id first), with the `school`
role, since the column documented who *ran the course* - not `dive_center`, which a club or a
university that taught it is not. Every course and every card is then linked by that string,
hidden cards included, so a hidden card is linked where a live row made the contact and nowhere
else. A string only hidden cards carry goes with the column: a contact
the diver cannot trace to anything they can see would be the one row in their list with no
explanation. `d7a49b1c58e2` reads deleted rows too and says why; its reason - child rows that
need a parent before a `NOT NULL` - has no counterpart here.

**Hidden dive-form sets gain `contact_uuid` wherever they hide `course_uuid`.** A preset or an
account's own set is stored data in `DiveFormField` declaration order, where the new member
sits immediately after `course_uuid`. So it is inserted right there, which is canonical order
without this file carrying a frozen copy of the enum, and only where the course is hidden -
the member the Basic preset's reasoning names, a record the diver creates first. Every other
set is left alone.

**Offline rendering.** `tests/test_migrations.py` runs `upgrade head --sql` against no
database, so both data steps are guarded with `context.is_offline_mode()`; the rendered DDL
stays complete.

`downgrade()` re-adds the two columns, copies each linked contact's name back, removes
`contact_uuid` from every hidden set and drops the references and the table. It restores each
live row's string up to trimming - to the first spelling where a diver's differed only by case
- and loses a hidden card's string no live row shared, and whatever a contact gained
afterwards: its phone, email, website and address, and a dive's, service record's or part's
link to it.
"""

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import context, op
from uuid6 import uuid7

# revision identifiers, used by Alembic.
revision: str = "a9b7dc451f00"
down_revision: str | None = "ab9a5add4fee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The five references, as (table, column). Named constraints, because the name is what the
# routes match an `IntegrityError` on to answer "Contact not found." rather than a 500.
_REFERENCES = (
    ("dive", "contact_id"),
    ("course", "contact_id"),
    ("certification", "contact_id"),
    ("gear_service_record", "contact_id"),
    ("trip_part", "accommodation_contact_id"),
)

# One row per diver and distinct string, carrying the first spelling seen: courses before
# certifications, lowest id first. Certifications are read live only (see the docstring).
_CONTACTS_TO_CREATE = sa.text(
    """
    SELECT DISTINCT ON (user_id, lower(btrim(training_center)))
           user_id,
           btrim(training_center) AS name
    FROM (
        SELECT user_id, training_center, 0 AS source, id FROM course
        UNION ALL
        SELECT user_id, training_center, 1 AS source, id FROM certification WHERE is_deleted = false
    ) AS strings
    WHERE btrim(coalesce(training_center, '')) <> ''
    ORDER BY user_id, lower(btrim(training_center)), source, id
    """
)

_INSERT_CONTACT = sa.text(
    """
    INSERT INTO contact (user_id, name, roles, notes, uuid, created_at)
    VALUES (:user_id, :name, CAST('["school"]' AS json), '', :uuid, :created_at)
    """
)

# Every row, hidden cards included, joined on the string the contact was made from. The
# contacts matched here are all this revision's: the table was created above.
_LINK_TO_CONTACT = """
    UPDATE {table} AS host
    SET contact_id = contact.id
    FROM contact
    WHERE contact.user_id = host.user_id
      AND lower(contact.name) = lower(btrim(host.training_center))
"""

_RESTORE_NAME = """
    UPDATE {table} AS host
    SET training_center = contact.name
    FROM contact
    WHERE contact.id = host.contact_id
"""

# (table, column) holding a hidden dive-form set, as a JSON list of `DiveFormField` values.
_HIDDEN_SETS = (("dive_form_preset", "hidden_fields"), ("user", "dive_form_hidden_fields"))
_COURSE = "course_uuid"
_CONTACT = "contact_uuid"


def _backfill_contacts() -> int:
    """Create the contacts the training-center strings name, then link every row. Returns
    how many were created.

    A uuid per row from `uuid7`, as `d7a49b1c58e2` chose over `gen_random_uuid()`: every
    public identifier in this schema is time-ordered, and a v4 would be the exception.
    """
    connection = op.get_bind()
    rows = connection.execute(_CONTACTS_TO_CREATE).mappings().all()
    if rows:
        created_at = datetime.now(UTC)
        connection.execute(
            _INSERT_CONTACT,
            [
                {"user_id": row["user_id"], "name": row["name"], "uuid": uuid7(), "created_at": created_at}
                for row in rows
            ],
        )
    for table in ("course", "certification"):
        connection.execute(sa.text(_LINK_TO_CONTACT.format(table=table)))
    return len(rows)


def _with_contact(hidden: list[str]) -> list[str]:
    """The set with `contact_uuid` placed right after `course_uuid`, its declared neighbour."""
    if _COURSE not in hidden or _CONTACT in hidden:
        return hidden
    at = hidden.index(_COURSE) + 1
    return [*hidden[:at], _CONTACT, *hidden[at:]]


def _without_contact(hidden: list[str]) -> list[str]:
    return [field for field in hidden if field != _CONTACT]


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
    op.create_table(
        "contact",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("roles", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("website", sa.String(length=512), nullable=True),
        sa.Column("address_street", sa.String(length=255), nullable=True),
        sa.Column("address_city", sa.String(length=255), nullable=True),
        sa.Column("address_postcode", sa.String(length=32), nullable=True),
        sa.Column("address_region", sa.String(length=255), nullable=True),
        sa.Column("address_country", sa.String(length=255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("uuid", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "address_country IS NOT NULL OR (address_street IS NULL AND address_city IS NULL "
            "AND address_postcode IS NULL AND address_region IS NULL)",
            name="ck_contact_address_has_country",
        ),
        # Cascade from the outset, with a plain index - see `257c6ae0d5bb`'s header.
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_contact_user_id"), "contact", ["user_id"], unique=False)
    op.create_index(op.f("ix_contact_uuid"), "contact", ["uuid"], unique=True)
    op.create_index("ix_contact_user_id_name", "contact", ["user_id", "name"], unique=False)
    op.create_index(
        "ux_contact_user_id_name_lower", "contact", ["user_id", sa.literal_column("lower(name)")], unique=True
    )

    for table, column in _REFERENCES:
        op.add_column(table, sa.Column(column, sa.Integer(), nullable=True))
        op.create_index(op.f(f"ix_{table}_{column}"), table, [column], unique=False)
        op.create_foreign_key(f"{table}_{column}_fkey", table, "contact", [column], ["id"], ondelete="SET NULL")

    if not context.is_offline_mode():
        _backfill_contacts()

    op.drop_column("course", "training_center")
    op.drop_column("certification", "training_center")

    if not context.is_offline_mode():
        _rewrite_hidden_sets(_with_contact, only_containing=_COURSE)


def downgrade() -> None:
    op.add_column("certification", sa.Column("training_center", sa.String(length=255), nullable=True))
    op.add_column("course", sa.Column("training_center", sa.String(length=255), nullable=True))

    if not context.is_offline_mode():
        for table in ("course", "certification"):
            op.get_bind().execute(sa.text(_RESTORE_NAME.format(table=table)))
        _rewrite_hidden_sets(_without_contact, only_containing=_CONTACT)

    for table, column in reversed(_REFERENCES):
        op.drop_constraint(f"{table}_{column}_fkey", table, type_="foreignkey")
        op.drop_index(op.f(f"ix_{table}_{column}"), table_name=table)
        op.drop_column(table, column)

    op.drop_index("ux_contact_user_id_name_lower", table_name="contact")
    op.drop_index("ix_contact_user_id_name", table_name="contact")
    op.drop_index(op.f("ix_contact_uuid"), table_name="contact")
    op.drop_index(op.f("ix_contact_user_id"), table_name="contact")
    op.drop_table("contact")
