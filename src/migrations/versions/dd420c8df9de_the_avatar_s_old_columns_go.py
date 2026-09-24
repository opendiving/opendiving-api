"""the avatar's old columns go

Revision ID: dd420c8df9de
Revises: 272bb184cbdd
Create Date: 2026-09-24 16:52:31.079252

`user.avatar_storage_key` and `avatar_sha256` go, with their index, leaving `user_picture`
the avatar's only record. Since `272bb184cbdd` every avatar change writes both, but the build
before it, still serving while that revision deployed, wrote the columns alone, so the two can
disagree. `RECONCILE` settles each case in the columns' favour, theirs being the later write:

- **A removed avatar whose row outlived it**: the row goes.
- **A replaced avatar whose row names the file that build unlinked**: the row takes the
  columns' rendition under a fresh uuid, and drops its original and crop, since that build
  kept no original and neither frames the new file.
- **A first avatar with no row**: it gets a rendition-only row, as `272bb184cbdd` gave every
  avatar stored before it.

A file only a dropped original or a deleted row named is then referenced by nothing, which is
what `src/scripts/sweep_orphaned_files.py` is for.

`downgrade()` re-adds the columns, nullable, and copies each avatar's rendition back into them
(`RESTORE`), which is what `272bb184cbdd` leaves them holding.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "dd420c8df9de"
down_revision: str | None = "272bb184cbdd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# An account holds an avatar in the columns when both are set, as `272bb184cbdd`'s backfill
# read them; the build that wrote them always set the two together.
_HELD = '"user".avatar_storage_key IS NOT NULL AND "user".avatar_sha256 IS NOT NULL'

RECONCILE = (
    f"""
DELETE FROM user_picture
USING "user"
WHERE user_picture.user_id = "user".id AND user_picture.kind = 'avatar' AND NOT ({_HELD})
""",
    f"""
UPDATE user_picture
SET uuid = gen_random_uuid(),
    rendition_storage_key = "user".avatar_storage_key,
    rendition_sha256 = "user".avatar_sha256,
    original_storage_key = NULL,
    original_sha256 = NULL,
    original_byte_size = NULL,
    original_content_type = NULL,
    original_filename = NULL,
    crop_x = NULL,
    crop_y = NULL,
    crop_width = NULL,
    crop_height = NULL,
    updated_at = now()
FROM "user"
WHERE user_picture.user_id = "user".id AND user_picture.kind = 'avatar' AND {_HELD}
  AND (
    user_picture.rendition_storage_key <> "user".avatar_storage_key
    OR user_picture.rendition_sha256 <> "user".avatar_sha256
  )
""",
    f"""
INSERT INTO user_picture (uuid, user_id, kind, rendition_storage_key, rendition_sha256, created_at)
SELECT gen_random_uuid(), "user".id, 'avatar', "user".avatar_storage_key, "user".avatar_sha256, now()
FROM "user"
WHERE {_HELD}
  AND NOT EXISTS (
    SELECT 1 FROM user_picture WHERE user_picture.user_id = "user".id AND user_picture.kind = 'avatar'
  )
""",
)

RESTORE = """
UPDATE "user"
SET avatar_storage_key = user_picture.rendition_storage_key, avatar_sha256 = user_picture.rendition_sha256
FROM user_picture
WHERE user_picture.user_id = "user".id AND user_picture.kind = 'avatar'
"""


def upgrade() -> None:
    for statement in RECONCILE:
        op.execute(statement)
    op.drop_index("ux_user_avatar_storage_key", table_name="user")
    op.drop_column("user", "avatar_sha256")
    op.drop_column("user", "avatar_storage_key")


def downgrade() -> None:
    op.add_column("user", sa.Column("avatar_storage_key", sa.String(length=255), nullable=True))
    op.add_column("user", sa.Column("avatar_sha256", sa.String(length=64), nullable=True))
    op.execute(RESTORE)
    op.create_index("ux_user_avatar_storage_key", "user", ["avatar_storage_key"], unique=True)
