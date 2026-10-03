"""map tiles, shared by every account, in place of map pictures

Revision ID: bb0a5f425d41
Revises: 53ea55b922e5
Create Date: 2026-10-03 16:00:00.000000

Drops `map_picture` and creates `map_tile`. Nothing is copied: a picture was one record's map
with its pins drawn in, and no part of one is a tile. Tiles are drawn as they are first asked
for. The files the dropped rows named, under `map-pictures/`, are orphans the sweeper's
`--delete` reclaims past its grace window - with `--force` where they are more than a quarter
of the store, which `--delete` otherwise refuses.

`downgrade()` drops `map_tile` and recreates an empty `map_picture`; the tiles' files are
then orphans the same way.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bb0a5f425d41"
down_revision: str | None = "53ea55b922e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ux_map_picture_user_id_digest_theme", table_name="map_picture")
    op.drop_index("ux_map_picture_storage_key", table_name="map_picture")
    op.drop_index("ix_map_picture_last_served_at", table_name="map_picture")
    op.drop_table("map_picture")

    op.create_table(
        "map_tile",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("z", sa.SmallInteger(), nullable=False),
        sa.Column("x", sa.Integer(), nullable=False),
        sa.Column("y", sa.Integer(), nullable=False),
        sa.Column("theme", sa.String(length=8), nullable=False),
        sa.Column("signature", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=255), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_served_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("theme IN ('light', 'dark')", name="ck_map_tile_theme"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_map_tile_last_served_at", "map_tile", ["last_served_at"], unique=False)
    op.create_index("ux_map_tile_storage_key", "map_tile", ["storage_key"], unique=True)
    op.create_index("ux_map_tile_signature_theme_z_x_y", "map_tile", ["signature", "theme", "z", "x", "y"], unique=True)


def downgrade() -> None:
    op.drop_index("ux_map_tile_signature_theme_z_x_y", table_name="map_tile")
    op.drop_index("ux_map_tile_storage_key", table_name="map_tile")
    op.drop_index("ix_map_tile_last_served_at", table_name="map_tile")
    op.drop_table("map_tile")

    op.create_table(
        "map_picture",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("theme", sa.String(length=8), nullable=False),
        sa.Column("storage_key", sa.String(length=255), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_served_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("theme IN ('light', 'dark')", name="ck_map_picture_theme"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_map_picture_last_served_at", "map_picture", ["last_served_at"], unique=False)
    op.create_index("ux_map_picture_storage_key", "map_picture", ["storage_key"], unique=True)
    op.create_index("ux_map_picture_user_id_digest_theme", "map_picture", ["user_id", "digest", "theme"], unique=True)
