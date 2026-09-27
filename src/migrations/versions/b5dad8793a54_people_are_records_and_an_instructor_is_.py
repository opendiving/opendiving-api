"""people are records, and an instructor is one

Revision ID: b5dad8793a54
Revises: a8fb7217f84a
Create Date: 2026-09-27 08:45:22.658072

`person` is an individual a diver was with, listed with a role from a dive, a trip and a
course through `dive_person`, `trip_person` and `course_person`, and named by a
certification as its `instructor_id`. The two `instructor_name` strings on `course` and
`certification` become rows of it and go; `instructor_number` stays on both. Autogenerate
drafted the DDL; the data steps are hand-written and it sees none of them.

**The backfill reads live rows only**, as `a9b7dc451f00` did for the training centers. One
person per distinct trimmed, lowercased string per diver, across every course and every
certification that is not soft-deleted - named with the first spelling seen, trimmed
(courses before cards, lowest id first). Every course is then listed with that person at
position 0 with the role `instructor`, and every card names it as `instructor_id`, hidden
cards included where a live row made the person. A string only hidden cards carry goes with
the column, for the reason that revision gives: a person the diver cannot trace to anything
they can see would be the one row in their list with no explanation. No dive names anyone,
so every backfilled person counts zero dives.

**Hidden dive-form sets gain `people` wherever they hide `contact_uuid`**, immediately after
it - `DiveFormField`'s canonical position, and the step `a9b7dc451f00` took for the contact -
so the Basic preset and any set built from it hide the new field. Every other set is left
alone.

**Offline rendering.** `tests/test_migrations.py` runs `upgrade head --sql` against no
database, so every data step is guarded with `context.is_offline_mode()`.

`downgrade()` re-adds the two columns, copies each course's first instructor's name and each
card's instructor's name back, removes `people` from every hidden set and drops the
references and the tables. It restores each live row's string up to trimming - to the first
spelling where a diver's differed only by case - and loses a hidden card's string no live
row shared, and everything people gained afterwards.
"""

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import context, op
from uuid6 import uuid7

# revision identifiers, used by Alembic.
revision: str = "b5dad8793a54"
down_revision: str | None = "a8fb7217f84a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# One row per diver and distinct string, carrying the first spelling seen: courses before
# certifications, lowest id first. Certifications are read live only (see the docstring).
_PEOPLE_TO_CREATE = sa.text(
    """
    SELECT DISTINCT ON (user_id, lower(btrim(instructor_name)))
           user_id,
           btrim(instructor_name) AS name
    FROM (
        SELECT user_id, instructor_name, 0 AS source, id FROM course
        UNION ALL
        SELECT user_id, instructor_name, 1 AS source, id FROM certification WHERE is_deleted = false
    ) AS strings
    WHERE btrim(coalesce(instructor_name, '')) <> ''
    ORDER BY user_id, lower(btrim(instructor_name)), source, id
    """
)

_INSERT_PERSON = sa.text(
    """
    INSERT INTO person (user_id, name, notes, uuid, created_at)
    VALUES (:user_id, :name, '', :uuid, :created_at)
    """
)

# Every course, joined on the string its person was made from. The people matched here are
# all this revision's: the table was created above.
_LIST_COURSE_INSTRUCTORS = sa.text(
    """
    INSERT INTO course_person (course_id, person_id, position, role)
    SELECT course.id, person.id, 0, 'instructor'
    FROM course
    JOIN person ON person.user_id = course.user_id
               AND lower(person.name) = lower(btrim(course.instructor_name))
    """
)

# Every card, hidden ones included.
_LINK_CERTIFICATION_INSTRUCTORS = sa.text(
    """
    UPDATE certification
    SET instructor_id = person.id
    FROM person
    WHERE person.user_id = certification.user_id
      AND lower(person.name) = lower(btrim(certification.instructor_name))
    """
)

_RESTORE_COURSE_NAMES = sa.text(
    """
    UPDATE course
    SET instructor_name = (
        SELECT person.name
        FROM course_person
        JOIN person ON person.id = course_person.person_id
        WHERE course_person.course_id = course.id AND course_person.role = 'instructor'
        ORDER BY course_person.position
        LIMIT 1
    )
    """
)

_RESTORE_CERTIFICATION_NAMES = sa.text(
    """
    UPDATE certification
    SET instructor_name = person.name
    FROM person
    WHERE person.id = certification.instructor_id
    """
)

# (table, column) holding a hidden dive-form set, as a JSON list of `DiveFormField` values.
_HIDDEN_SETS = (("dive_form_preset", "hidden_fields"), ("user", "dive_form_hidden_fields"))
_CONTACT = "contact_uuid"
_PEOPLE = "people"

_JOIN_TABLES = (("dive_person", "dive"), ("trip_person", "trip"), ("course_person", "course"))


def _backfill_people() -> None:
    """Create the people the instructor strings name, then list and link every row.

    A uuid per row from `uuid7`, as every public identifier in this schema is time-ordered.
    """
    connection = op.get_bind()
    rows = connection.execute(_PEOPLE_TO_CREATE).mappings().all()
    if rows:
        created_at = datetime.now(UTC)
        connection.execute(
            _INSERT_PERSON,
            [
                {"user_id": row["user_id"], "name": row["name"], "uuid": uuid7(), "created_at": created_at}
                for row in rows
            ],
        )
    connection.execute(_LIST_COURSE_INSTRUCTORS)
    connection.execute(_LINK_CERTIFICATION_INSTRUCTORS)


def _with_people(hidden: list[str]) -> list[str]:
    """The set with `people` placed right after `contact_uuid`, its declared neighbour."""
    if _CONTACT not in hidden or _PEOPLE in hidden:
        return hidden
    at = hidden.index(_CONTACT) + 1
    return [*hidden[:at], _PEOPLE, *hidden[at:]]


def _without_people(hidden: list[str]) -> list[str]:
    return [field for field in hidden if field != _PEOPLE]


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
        "person",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("linked_user_id", sa.Integer(), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("uuid", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("linked_user_id <> user_id", name="ck_person_not_linked_to_its_owner"),
        # The owner cascades; the linked account, being somebody else, only unlinks.
        sa.ForeignKeyConstraint(["linked_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_person_linked_user_id"), "person", ["linked_user_id"], unique=False)
    op.create_index(op.f("ix_person_user_id"), "person", ["user_id"], unique=False)
    op.create_index("ix_person_user_id_name", "person", ["user_id", "name"], unique=False)
    op.create_index(op.f("ix_person_uuid"), "person", ["uuid"], unique=True)
    op.create_index(
        "ux_person_user_id_linked_user_id",
        "person",
        ["user_id", "linked_user_id"],
        unique=True,
        postgresql_where=sa.text("linked_user_id IS NOT NULL"),
    )
    op.create_index(
        "ux_person_user_id_name_lower", "person", ["user_id", sa.literal_column("lower(name)")], unique=True
    )

    for table, host in _JOIN_TABLES:
        op.create_table(
            table,
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column(f"{host}_id", sa.Integer(), nullable=False),
            sa.Column("person_id", sa.Integer(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("role", sa.String(length=16), nullable=True),
            sa.ForeignKeyConstraint([f"{host}_id"], [f"{host}.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["person_id"], ["person.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(f"{host}_id", "person_id", name=f"ux_{table}_{host}_id_person_id"),
        )
        op.create_index(op.f(f"ix_{table}_person_id"), table, ["person_id"], unique=False)
        op.create_index(f"ix_{table}_{host}_id_position", table, [f"{host}_id", "position"], unique=False)

    op.add_column("certification", sa.Column("instructor_id", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_certification_instructor_id"), "certification", ["instructor_id"], unique=False)
    # Named, because the name is what the route matches an `IntegrityError` on.
    op.create_foreign_key(
        "certification_instructor_id_fkey", "certification", "person", ["instructor_id"], ["id"], ondelete="SET NULL"
    )

    if not context.is_offline_mode():
        _backfill_people()

    op.drop_column("certification", "instructor_name")
    op.drop_column("course", "instructor_name")

    if not context.is_offline_mode():
        _rewrite_hidden_sets(_with_people, only_containing=_CONTACT)


def downgrade() -> None:
    op.add_column("course", sa.Column("instructor_name", sa.String(length=255), nullable=True))
    op.add_column("certification", sa.Column("instructor_name", sa.String(length=255), nullable=True))

    if not context.is_offline_mode():
        op.get_bind().execute(_RESTORE_COURSE_NAMES)
        op.get_bind().execute(_RESTORE_CERTIFICATION_NAMES)
        _rewrite_hidden_sets(_without_people, only_containing=_PEOPLE)

    op.drop_constraint("certification_instructor_id_fkey", "certification", type_="foreignkey")
    op.drop_index(op.f("ix_certification_instructor_id"), table_name="certification")
    op.drop_column("certification", "instructor_id")

    for table, host in reversed(_JOIN_TABLES):
        op.drop_index(f"ix_{table}_{host}_id_position", table_name=table)
        op.drop_index(op.f(f"ix_{table}_person_id"), table_name=table)
        op.drop_table(table)

    op.drop_index("ux_person_user_id_name_lower", table_name="person")
    op.drop_index(
        "ux_person_user_id_linked_user_id", table_name="person", postgresql_where=sa.text("linked_user_id IS NOT NULL")
    )
    op.drop_index(op.f("ix_person_uuid"), table_name="person")
    op.drop_index("ix_person_user_id_name", table_name="person")
    op.drop_index(op.f("ix_person_user_id"), table_name="person")
    op.drop_index(op.f("ix_person_linked_user_id"), table_name="person")
    op.drop_table("person")
