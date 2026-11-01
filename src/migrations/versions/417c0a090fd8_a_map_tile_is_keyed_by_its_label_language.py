"""a map tile is keyed by its label language

`map_tile.language` is the second language a tile's labels carry, NULL for English alone, and
joins the unique key under NULLS NOT DISTINCT so English alone is one tile per square as well.
Every existing row becomes an English-alone tile under the signature it was drawn with. The
renderer that draws English alone names a new signature, so those rows are served by nobody
and the worker deletes them past `UNSERVED_RETENTION`, as it does every redeployed renderer's.

`downgrade()` drops the column, and with it the tiles stored in a language: deleted first, so
the old key holds, and their files left as orphans the sweeper's `--delete` reclaims.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "417c0a090fd8"
down_revision: str | None = "9ff39bc7568b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("map_tile", sa.Column("language", sa.String(length=8), nullable=True))
    op.drop_index("ux_map_tile_signature_theme_z_x_y", table_name="map_tile")
    op.create_index(
        "ux_map_tile_signature_theme_language_z_x_y",
        "map_tile",
        ["signature", "theme", "language", "z", "x", "y"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.drop_index("ux_map_tile_signature_theme_language_z_x_y", table_name="map_tile")
    op.execute("DELETE FROM map_tile WHERE language IS NOT NULL")
    op.drop_column("map_tile", "language")
    op.create_index("ux_map_tile_signature_theme_z_x_y", "map_tile", ["signature", "theme", "z", "x", "y"], unique=True)
