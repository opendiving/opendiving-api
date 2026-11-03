"""a dive-computer original keeps its tag, and a profile names every reader that fed it

`dive_file` gains the libdivecomputer descriptor - vendor, product and model number - and the
fallback start a dive-computer original the iOS app downloads is tagged with, which its reader
needs beside the bytes. All nullable, and NULL on every existing row: no stored file was tagged,
and those stay originals this instance does not read.

`dive_profile.reader_version` widens to 64 characters, since a profile two readers fed records
both versions. Widening a `varchar` rewrites no row.

`downgrade()` drops the columns, and with them every tag: those files can no longer be read, so
it re-keys them to `divejson_import`, the key of a stored file nothing reads. A reader version
past the old width is cleared, which marks its profile for the next backfill.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3b5134d444b3"
down_revision: str | None = "417c0a090fd8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("dive_file", sa.Column("descriptor_vendor", sa.String(length=64), nullable=True))
    op.add_column("dive_file", sa.Column("descriptor_product", sa.String(length=64), nullable=True))
    op.add_column("dive_file", sa.Column("descriptor_model", sa.Integer(), nullable=True))
    op.add_column("dive_file", sa.Column("fallback_start", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_dive_file_descriptor_whole",
        "dive_file",
        "(descriptor_vendor IS NULL) = (descriptor_product IS NULL) "
        "AND (descriptor_vendor IS NULL) = (descriptor_model IS NULL)",
    )
    op.create_check_constraint("ck_dive_file_descriptor_model", "dive_file", "descriptor_model >= 0")
    op.create_check_constraint(
        "ck_dive_file_fallback_start_tagged", "dive_file", "fallback_start IS NULL OR descriptor_vendor IS NOT NULL"
    )
    op.alter_column(
        "dive_profile",
        "reader_version",
        existing_type=sa.String(length=32),
        type_=sa.String(length=64),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.execute("UPDATE dive_profile SET reader_version = NULL WHERE length(reader_version) > 32")
    op.alter_column(
        "dive_profile",
        "reader_version",
        existing_type=sa.String(length=64),
        type_=sa.String(length=32),
        existing_nullable=True,
    )
    op.execute(
        "UPDATE dive_file SET parser_key = 'divejson_import', content_type = 'application/octet-stream' "
        "WHERE parser_key = 'libdivecomputer'"
    )
    op.drop_constraint("ck_dive_file_fallback_start_tagged", "dive_file", type_="check")
    op.drop_constraint("ck_dive_file_descriptor_model", "dive_file", type_="check")
    op.drop_constraint("ck_dive_file_descriptor_whole", "dive_file", type_="check")
    op.drop_column("dive_file", "fallback_start")
    op.drop_column("dive_file", "descriptor_model")
    op.drop_column("dive_file", "descriptor_product")
    op.drop_column("dive_file", "descriptor_vendor")
