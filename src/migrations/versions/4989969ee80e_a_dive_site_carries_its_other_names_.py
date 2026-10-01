"""a dive site carries its other names, external ids, depth range, water type, altitude, entry types and tags

Revision ID: 4989969ee80e
Revises: 14395821e42b
Create Date: 2026-10-01 09:54:34.315447

A dive site gains `other_names`, `external_ids`, `depth_from`, `depth_to`, `water_type`,
`altitude` and `entry_types`, and `dive_site_tag` lists the diver's tags on sites as
`dive_tag` lists them on dives. The three lists are empty and the four scalars null on every
existing row, so there is no data step and every site reads as it did. The four `CHECK`s are
written by hand, as `e8b670ac5780` wrote its own: autogenerate does not see one added to an
existing table.

No identity is written for a site picked from the catalogue before this revision: nothing
recorded which row it came from.

`downgrade()` drops the table and the columns, which discards every value they held.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4989969ee80e"
down_revision: str | None = "14395821e42b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dive_site_tag",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dive_site_id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["dive_site_id"], ["dive_site.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tag_id"], ["tag.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dive_site_id", "tag_id", name="ux_dive_site_tag_dive_site_id_tag_id"),
    )
    op.create_index(
        "ix_dive_site_tag_dive_site_id_position", "dive_site_tag", ["dive_site_id", "position"], unique=False
    )
    op.create_index(op.f("ix_dive_site_tag_tag_id"), "dive_site_tag", ["tag_id"], unique=False)
    op.add_column("dive_site", sa.Column("other_names", sa.JSON(), server_default="[]", nullable=False))
    op.add_column("dive_site", sa.Column("external_ids", sa.JSON(), server_default="[]", nullable=False))
    op.add_column("dive_site", sa.Column("depth_from", sa.Float(), nullable=True))
    op.add_column("dive_site", sa.Column("depth_to", sa.Float(), nullable=True))
    op.add_column("dive_site", sa.Column("water_type", sa.String(length=32), nullable=True))
    op.add_column("dive_site", sa.Column("altitude", sa.Integer(), nullable=True))
    op.add_column("dive_site", sa.Column("entry_types", sa.JSON(), server_default="[]", nullable=False))
    op.create_check_constraint(
        "ck_dive_site_depth_from_non_negative", "dive_site", "depth_from IS NULL OR depth_from >= 0"
    )
    op.create_check_constraint("ck_dive_site_depth_to_non_negative", "dive_site", "depth_to IS NULL OR depth_to >= 0")
    op.create_check_constraint(
        "ck_dive_site_depth_range", "dive_site", "depth_from IS NULL OR depth_to IS NULL OR depth_from <= depth_to"
    )
    op.create_check_constraint(
        "ck_dive_site_altitude_range", "dive_site", "altitude IS NULL OR (altitude >= -450 AND altitude <= 6500)"
    )


def downgrade() -> None:
    op.drop_constraint("ck_dive_site_altitude_range", "dive_site", type_="check")
    op.drop_constraint("ck_dive_site_depth_range", "dive_site", type_="check")
    op.drop_constraint("ck_dive_site_depth_to_non_negative", "dive_site", type_="check")
    op.drop_constraint("ck_dive_site_depth_from_non_negative", "dive_site", type_="check")
    op.drop_column("dive_site", "entry_types")
    op.drop_column("dive_site", "altitude")
    op.drop_column("dive_site", "water_type")
    op.drop_column("dive_site", "depth_to")
    op.drop_column("dive_site", "depth_from")
    op.drop_column("dive_site", "external_ids")
    op.drop_column("dive_site", "other_names")
    op.drop_index(op.f("ix_dive_site_tag_tag_id"), table_name="dive_site_tag")
    op.drop_index("ix_dive_site_tag_dive_site_id_position", table_name="dive_site_tag")
    op.drop_table("dive_site_tag")
