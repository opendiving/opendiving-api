"""avatars on the files volume, replacing profile_image_url

Two nullable columns naming a blob on the files volume, and the removal of the column they
replace. `profile_image_url` was boilerplate residue: it defaulted to the fictional
`https://profileimageurl.com`, took Google's `picture` URL on a Google sign-up, and was
rendered by nothing anywhere. See *"Avatars are the third kind on the files volume"* in
`DECISIONS.md`.

**No data move, in either direction.** Nothing has ever been stored under the dropped
column that an avatar could be derived from - a Google CDN URL is not something this
instance owns, and re-fetching one at migration time would be a network call inside a
schema change. Existing accounts come out of this with no picture, which is the initials
fallback the clients already draw.

`avatar_storage_key` gets the same unique index `dive_file` and `certification_file` carry
on theirs: two rows naming one key would let either one's replacement unlink the other's
bytes. Nullable, and Postgres allows any number of NULLs in a unique index, so every
account without a picture is unaffected.

The downgrade re-adds `profile_image_url` through a `server_default` it then drops. The
column is `NOT NULL` and the table has rows, so adding it bare is a statement Postgres
refuses - and a downgrade that cannot run is worse than none, because it is discovered
halfway through one. It leaves every stored avatar on the volume as an unreferenced file,
which is what `src/scripts/sweep_orphaned_files.py` is for; unlinking them from a schema
migration would make a downgrade destroy data the re-upgrade cannot restore.

Revision ID: e417293f4502
Revises: 48781087b2b3
Create Date: 2026-08-21 08:01:56.220768

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e417293f4502"
down_revision: str | None = "48781087b2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# What the dropped column held for an account with no picture. Frozen here rather than
# imported: this value is history the moment this revision merges.
_PLACEHOLDER_IMAGE_URL = "https://profileimageurl.com"


def upgrade() -> None:
    op.add_column("user", sa.Column("avatar_storage_key", sa.String(length=255), nullable=True))
    op.add_column("user", sa.Column("avatar_sha256", sa.String(length=64), nullable=True))
    op.create_index("ux_user_avatar_storage_key", "user", ["avatar_storage_key"], unique=True)
    op.drop_column("user", "profile_image_url")


def downgrade() -> None:
    op.add_column(
        "user",
        sa.Column("profile_image_url", sa.VARCHAR(), nullable=False, server_default=_PLACEHOLDER_IMAGE_URL),
    )
    op.alter_column("user", "profile_image_url", server_default=None)
    op.drop_index("ux_user_avatar_storage_key", table_name="user")
    op.drop_column("user", "avatar_sha256")
    op.drop_column("user", "avatar_storage_key")
