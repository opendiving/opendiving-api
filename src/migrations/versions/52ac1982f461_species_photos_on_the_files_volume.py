"""species photos on the files volume

Eight nullable columns on `species`: where one Wikimedia Commons photograph is stored, which
version it is, and the parts a compliant credit line is built from. Additive only - nothing is
dropped, nothing is rewritten, and every existing row comes out of this with no photo, which is
the state most of them will keep. See *"Species photos are the fourth kind on the files volume"*
in `DECISIONS.md`.

**No data backfill here, deliberately.** Filling these needs two outbound calls per species to
third-party APIs, which is not something a schema migration may do - a container start would
then hang on Wikimedia being reachable. `src/scripts/backfill_species_photos.py` is the route,
and `photo_fetched_at IS NULL` - the state every row is left in below - is exactly the predicate
it selects on.

`photo_storage_key` gets the same unique index `dive_file`, `certification_file` and `user`
carry on theirs: two rows naming one key would let either one's replacement unlink the other's
bytes. Nullable, and Postgres allows any number of NULLs in a unique index, so the photo-less
majority is unaffected.

The downgrade leaves every stored photo on the volume as an unreferenced file, which is what
`src/scripts/sweep_orphaned_files.py` is for. Unlinking them from a schema migration would make
a downgrade destroy bytes the re-upgrade cannot restore - the same call the avatar revision made
for the same reason.

Revision ID: 52ac1982f461
Revises: 8773a3c53fc9
Create Date: 2026-08-31 18:51:43.005541

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "52ac1982f461"
down_revision: str | None = "8773a3c53fc9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("species", sa.Column("photo_storage_key", sa.String(length=255), nullable=True))
    op.add_column("species", sa.Column("photo_sha256", sa.String(length=64), nullable=True))
    op.add_column("species", sa.Column("photo_file", sa.String(length=255), nullable=True))
    op.add_column("species", sa.Column("photo_author", sa.String(length=255), nullable=True))
    op.add_column("species", sa.Column("photo_license", sa.String(length=128), nullable=True))
    op.add_column("species", sa.Column("photo_license_url", sa.String(length=512), nullable=True))
    op.add_column("species", sa.Column("photo_source_url", sa.String(length=512), nullable=True))
    op.add_column("species", sa.Column("photo_fetched_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ux_species_photo_storage_key", "species", ["photo_storage_key"], unique=True)


def downgrade() -> None:
    op.drop_index("ux_species_photo_storage_key", table_name="species")
    op.drop_column("species", "photo_fetched_at")
    op.drop_column("species", "photo_source_url")
    op.drop_column("species", "photo_license_url")
    op.drop_column("species", "photo_license")
    op.drop_column("species", "photo_author")
    op.drop_column("species", "photo_file")
    op.drop_column("species", "photo_sha256")
    op.drop_column("species", "photo_storage_key")
