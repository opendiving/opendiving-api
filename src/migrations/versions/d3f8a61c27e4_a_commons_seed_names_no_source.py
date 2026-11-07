"""A Commons seed's constraint states the property and names no source

Revision ID: d3f8a61c27e4
Revises: b4d27e6f1c03
Create Date: 2026-11-07 10:00:00.000000

`ck_dive_site_owner_provenance` listed the sources a seed could have. It now says what any seed
carries, so what admits a source is the bundle's `sources` block alone. Every row the old one
allowed has a source: the bundle and a review are all that write one, and each writes it. No data
moves.

`downgrade()` puts the list back, and takes first the records it would refuse, with every dive's
link to them.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3f8a61c27e4"
down_revision: str | None = "b4d27e6f1c03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "ck_dive_site_owner_provenance"

_PRIVATE = (
    "(user_id IS NOT NULL AND commons_source IS NULL AND commons_license IS NULL "
    "AND commons_seed_registry IS NULL AND commons_seed_identifier IS NULL "
    "AND commons_retrieved IS NULL) "
)
_CONTRIBUTION = (
    "OR (commons_source = 'contribution' AND commons_seed_registry IS NULL "
    "AND commons_seed_identifier IS NULL AND commons_retrieved IS NULL)))"
)

OWNER_PROVENANCE = (
    f"{_PRIVATE}"
    "OR (user_id IS NULL AND commons_source IS NOT NULL AND commons_license IS NOT NULL AND ("
    "(commons_source <> 'contribution' AND commons_seed_registry IS NOT NULL "
    "AND commons_seed_identifier IS NOT NULL AND commons_retrieved IS NOT NULL) "
    f"{_CONTRIBUTION}"
)

_NAMED_SOURCES = (
    f"{_PRIVATE}"
    "OR (user_id IS NULL AND commons_license IS NOT NULL AND ("
    "(commons_source IN ('openstreetmap', 'wikidata') AND commons_seed_registry IS NOT NULL "
    "AND commons_seed_identifier IS NOT NULL AND commons_retrieved IS NOT NULL) "
    f"{_CONTRIBUTION}"
)


def upgrade() -> None:
    op.drop_constraint(_NAME, "dive_site", type_="check")
    op.create_check_constraint(_NAME, "dive_site", OWNER_PROVENANCE)


def downgrade() -> None:
    op.execute(
        "DELETE FROM dive_site WHERE user_id IS NULL "
        "AND commons_source NOT IN ('openstreetmap', 'wikidata', 'contribution')"
    )
    op.drop_constraint(_NAME, "dive_site", type_="check")
    op.create_check_constraint(_NAME, "dive_site", _NAMED_SOURCES)
