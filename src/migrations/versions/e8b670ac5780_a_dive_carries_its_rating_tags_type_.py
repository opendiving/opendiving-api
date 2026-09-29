"""a dive carries its rating, tags, type, conditions, entry type and boat name

Revision ID: e8b670ac5780
Revises: a84d94bb0272
Create Date: 2026-09-29 11:34:16.510254

A dive gains the diver's classification and conditions - `type`, `rating`,
`air_temperature`, `current`, `waves`, `weather`, `entry_type` and `boat_name` - every one
nullable and empty on every existing row, so there is no data step. `ck_dive_rating_range` is
written by hand, as `c47b308253a3` wrote its constraint: autogenerate does not see a `CHECK`
added to an existing table.

`tag` is the diver's own vocabulary and `dive_tag` lists it on dives. The tag's uniqueness is
`casefold(name COLLATE pg_unicode_fast)` - Unicode full case folding, which Postgres 18 is the
first release to ship - so two spellings DiveJSON calls one tag cannot both be rows.

Stored hidden dive-form sets are left alone: a changed default set reaches new accounts only.

`downgrade()` drops the tables and the columns, which discards every tag and every value these
columns held.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8b670ac5780"
down_revision: str | None = "a84d94bb0272"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tag",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("uuid", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tag_user_id"), "tag", ["user_id"], unique=False)
    op.create_index("ix_tag_user_id_name", "tag", ["user_id", "name"], unique=False)
    op.create_index(op.f("ix_tag_uuid"), "tag", ["uuid"], unique=True)
    op.create_index(
        "ux_tag_user_id_name_folded",
        "tag",
        ["user_id", sa.literal_column("casefold(name::text COLLATE pg_unicode_fast)")],
        unique=True,
    )
    op.create_table(
        "dive_tag",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dive_id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["dive_id"], ["dive.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tag_id"], ["tag.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dive_id", "tag_id", name="ux_dive_tag_dive_id_tag_id"),
    )
    op.create_index("ix_dive_tag_dive_id_position", "dive_tag", ["dive_id", "position"], unique=False)
    op.create_index(op.f("ix_dive_tag_tag_id"), "dive_tag", ["tag_id"], unique=False)
    op.add_column("dive", sa.Column("type", sa.String(length=32), nullable=True))
    op.add_column("dive", sa.Column("rating", sa.Integer(), nullable=True))
    op.add_column("dive", sa.Column("air_temperature", sa.Float(), nullable=True))
    op.add_column("dive", sa.Column("current", sa.String(length=32), nullable=True))
    op.add_column("dive", sa.Column("waves", sa.String(length=32), nullable=True))
    op.add_column("dive", sa.Column("weather", sa.String(length=32), nullable=True))
    op.add_column("dive", sa.Column("entry_type", sa.String(length=32), nullable=True))
    op.add_column("dive", sa.Column("boat_name", sa.String(length=255), nullable=True))
    op.create_check_constraint("ck_dive_rating_range", "dive", "rating IS NULL OR rating BETWEEN 1 AND 5")


def downgrade() -> None:
    op.drop_constraint("ck_dive_rating_range", "dive", type_="check")
    op.drop_column("dive", "boat_name")
    op.drop_column("dive", "entry_type")
    op.drop_column("dive", "weather")
    op.drop_column("dive", "waves")
    op.drop_column("dive", "current")
    op.drop_column("dive", "air_temperature")
    op.drop_column("dive", "rating")
    op.drop_column("dive", "type")
    op.drop_index(op.f("ix_dive_tag_tag_id"), table_name="dive_tag")
    op.drop_index("ix_dive_tag_dive_id_position", table_name="dive_tag")
    op.drop_table("dive_tag")
    op.drop_index("ux_tag_user_id_name_folded", table_name="tag")
    op.drop_index(op.f("ix_tag_uuid"), table_name="tag")
    op.drop_index("ix_tag_user_id_name", table_name="tag")
    op.drop_index(op.f("ix_tag_user_id"), table_name="tag")
    op.drop_table("tag")
