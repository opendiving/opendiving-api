"""add course and link it from dives and certifications

One new table plus one nullable FK on each of `dive` and `certification`. No backfill:
nothing existing can be derived into a course, and both links start null.

Three things about the table are deliberate rather than autogenerate's doing, and are the
reason this file is worth reading rather than skimming:

- **`ON DELETE CASCADE` on `user_id` from the outset**, with a plain (not partial) index.
  Autogenerate cannot detect an `ondelete` added later, and a partial index does not serve
  the cascade's RI lookup - both rules are spelled out in the header of `48781087b2b3`,
  which had to redeclare ten constraints by hand for want of the first.
- **`ON DELETE SET NULL` on both new FKs.** A course is a grouping, not an owner: deleting
  one leaves its dives and the certifications it issued in place, with the link cleared by
  the database. Same rule as `dive.trip_id`.
- **`ck_course_date_range`.** The API validates the pair on create and re-validates it
  against the stored row on PATCH, but CRUDAdmin writes through the pydantic update schema
  and can send one date alone, which slips past a both-present check. SQL NULL semantics
  make the constraint vacuous when either date is absent, which is exactly the wanted
  behaviour - a `planned` course with only an end date is a real state.

There is no unique index on `(user_id, lower(name))`, diverging from `trip`: a course
failed once and retaken later is legitimately the same name twice.

Revision ID: 257c6ae0d5bb
Revises: a400abb069ef
Create Date: 2026-08-28 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "257c6ae0d5bb"
down_revision: str | None = "a400abb069ef"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "course",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("agency", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("agency_other", sa.String(length=64), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("instructor_name", sa.String(length=255), nullable=True),
        sa.Column("instructor_number", sa.String(length=64), nullable=True),
        sa.Column("training_center", sa.String(length=255), nullable=True),
        sa.Column("cost", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("uuid", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("end_date >= start_date", name="ck_course_date_range"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_course_user_id"), "course", ["user_id"], unique=False)
    op.create_index(
        "ix_course_user_id_start_date",
        "course",
        ["user_id", sa.literal_column("start_date DESC NULLS LAST")],
        unique=False,
    )
    op.create_index(op.f("ix_course_uuid"), "course", ["uuid"], unique=True)

    op.add_column("certification", sa.Column("course_id", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_certification_course_id"), "certification", ["course_id"], unique=False)
    op.create_foreign_key(
        "certification_course_id_fkey", "certification", "course", ["course_id"], ["id"], ondelete="SET NULL"
    )

    op.add_column("dive", sa.Column("course_id", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_dive_course_id"), "dive", ["course_id"], unique=False)
    op.create_foreign_key("dive_course_id_fkey", "dive", "course", ["course_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    op.drop_constraint("dive_course_id_fkey", "dive", type_="foreignkey")
    op.drop_index(op.f("ix_dive_course_id"), table_name="dive")
    op.drop_column("dive", "course_id")

    op.drop_constraint("certification_course_id_fkey", "certification", type_="foreignkey")
    op.drop_index(op.f("ix_certification_course_id"), table_name="certification")
    op.drop_column("certification", "course_id")

    op.drop_index(op.f("ix_course_uuid"), table_name="course")
    op.drop_index("ix_course_user_id_start_date", table_name="course")
    op.drop_index(op.f("ix_course_user_id"), table_name="course")
    op.drop_table("course")
