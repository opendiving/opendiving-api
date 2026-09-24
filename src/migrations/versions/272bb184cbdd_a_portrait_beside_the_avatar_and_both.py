"""a portrait beside the avatar, and both keep their originals

Revision ID: 272bb184cbdd
Revises: b7c41e9d2a05
Create Date: 2026-09-24 08:00:00.000000

`user_picture` holds an account's avatar and its check-in portrait, one row per kind: the
rendition every screen shows, and the original it is rendered from with the crop that
frames it. `BACKFILL` gives every stored avatar a row naming the rendition key and digest
the `user` columns hold, unchanged, so each keeps loading from the same blob under the same
`ETag`; none of them has an original or a crop, since none was kept.

`avatar_storage_key` and `avatar_sha256` stay on `user`. The build serving while this runs
selects every column it maps on every signed-in request, so dropping them would fail each of
those requests until the switch; the new build keeps writing them beside the row and selects
them nowhere.

`downgrade()` copies each avatar's rendition back into the columns (`RESTORE`) and drops the
table, portraits and originals with it. Their blobs are then referenced by nothing, which is
what `src/scripts/sweep_orphaned_files.py` is for; unlinking them from a migration would make
a downgrade destroy what the re-upgrade cannot bring back.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "272bb184cbdd"
down_revision: str | None = "b7c41e9d2a05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORIGINAL_COLUMNS = (
    "original_storage_key",
    "original_sha256",
    "original_byte_size",
    "original_content_type",
    "original_filename",
    "crop_x",
    "crop_y",
    "crop_width",
    "crop_height",
)

BACKFILL = """
INSERT INTO user_picture (uuid, user_id, kind, rendition_storage_key, rendition_sha256, created_at)
SELECT gen_random_uuid(), id, 'avatar', avatar_storage_key, avatar_sha256, now()
FROM "user"
WHERE avatar_storage_key IS NOT NULL AND avatar_sha256 IS NOT NULL
"""

RESTORE = """
UPDATE "user"
SET avatar_storage_key = user_picture.rendition_storage_key, avatar_sha256 = user_picture.rendition_sha256
FROM user_picture
WHERE user_picture.user_id = "user".id AND user_picture.kind = 'avatar'
"""


def upgrade() -> None:
    every_null = " AND ".join(f"{column} IS NULL" for column in _ORIGINAL_COLUMNS)
    none_null = " AND ".join(f"{column} IS NOT NULL" for column in _ORIGINAL_COLUMNS)
    op.create_table(
        "user_picture",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("rendition_storage_key", sa.String(length=255), nullable=False),
        sa.Column("rendition_sha256", sa.String(length=64), nullable=False),
        sa.Column("original_storage_key", sa.String(length=255), nullable=True),
        sa.Column("original_sha256", sa.String(length=64), nullable=True),
        sa.Column("original_byte_size", sa.Integer(), nullable=True),
        sa.Column("original_content_type", sa.String(length=64), nullable=True),
        sa.Column("original_filename", sa.String(length=255), nullable=True),
        sa.Column("crop_x", sa.Integer(), nullable=True),
        sa.Column("crop_y", sa.Integer(), nullable=True),
        sa.Column("crop_width", sa.Integer(), nullable=True),
        sa.Column("crop_height", sa.Integer(), nullable=True),
        sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"({every_null}) OR ({none_null})", name="ck_user_picture_original_members_together"),
        sa.CheckConstraint(
            "crop_x IS NULL OR (crop_x >= 0 AND crop_y >= 0 AND crop_width > 0 AND crop_height > 0)",
            name="ck_user_picture_crop_positive",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_user_picture_uuid"), "user_picture", ["uuid"], unique=True)
    op.create_index("ux_user_picture_user_id_kind", "user_picture", ["user_id", "kind"], unique=True)
    op.create_index("ux_user_picture_rendition_storage_key", "user_picture", ["rendition_storage_key"], unique=True)
    op.create_index("ux_user_picture_original_storage_key", "user_picture", ["original_storage_key"], unique=True)
    op.execute(BACKFILL)


def downgrade() -> None:
    op.execute(RESTORE)
    op.drop_index("ux_user_picture_original_storage_key", table_name="user_picture")
    op.drop_index("ux_user_picture_rendition_storage_key", table_name="user_picture")
    op.drop_index("ux_user_picture_user_id_kind", table_name="user_picture")
    op.drop_index(op.f("ix_user_picture_uuid"), table_name="user_picture")
    op.drop_table("user_picture")
