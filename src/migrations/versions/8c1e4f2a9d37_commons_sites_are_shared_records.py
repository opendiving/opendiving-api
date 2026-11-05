"""OpenDiving Commons sites are shared records a dive names, not copies

Revision ID: 8c1e4f2a9d37
Revises: 63b752e6b762
Create Date: 2026-11-05 18:40:00.000000

`dive_site.user_id` becomes nullable, null meaning the Commons owns the row, and a Commons row
carries its provenance. The rows themselves arrive after the migrations, from the bundle.

`dive_site.notes` goes, once what only it holds is on `saved_site`. The previous revision's
overlap left two things behind, and this assumes the build it shipped is the one serving: a
private site the build before it inserted has no saved row, and a saved row whose `updated_at`
is null was never written by that build, so a column that says something else was edited by the
build before it. A row that build did write keeps what it holds, an emptied note included, and
an empty column is a site that build created.

`downgrade()` removes the Commons rows, with every dive's link to them, and copies each owner's
notes back. A saved row on a Commons record goes with the record.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8c1e4f2a9d37"
down_revision: str | None = "63b752e6b762"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OWNER_PROVENANCE = (
    "(user_id IS NOT NULL AND commons_source IS NULL AND commons_license IS NULL "
    "AND commons_seed_registry IS NULL AND commons_seed_identifier IS NULL "
    "AND commons_retrieved IS NULL) "
    "OR (user_id IS NULL AND commons_license IS NOT NULL AND ("
    "(commons_source IN ('openstreetmap', 'wikidata') AND commons_seed_registry IS NOT NULL "
    "AND commons_seed_identifier IS NOT NULL AND commons_retrieved IS NOT NULL) "
    "OR (commons_source = 'contribution' AND commons_seed_registry IS NULL "
    "AND commons_seed_identifier IS NULL AND commons_retrieved IS NULL)))"
)


def upgrade() -> None:
    op.execute(
        "INSERT INTO saved_site (user_id, dive_site_id, notes, created_at) "
        "SELECT dive_site.user_id, dive_site.id, dive_site.notes, dive_site.created_at FROM dive_site "
        "WHERE NOT EXISTS (SELECT 1 FROM saved_site WHERE saved_site.dive_site_id = dive_site.id "
        "AND saved_site.user_id = dive_site.user_id)"
    )
    op.execute(
        "UPDATE saved_site SET notes = dive_site.notes FROM dive_site "
        "WHERE saved_site.dive_site_id = dive_site.id AND saved_site.user_id = dive_site.user_id "
        "AND saved_site.updated_at IS NULL AND dive_site.notes <> '' AND dive_site.notes <> saved_site.notes"
    )
    op.drop_column("dive_site", "notes")

    op.alter_column("dive_site", "user_id", existing_type=sa.Integer(), nullable=True)
    op.add_column("dive_site", sa.Column("commons_source", sa.String(length=32), nullable=True))
    op.add_column("dive_site", sa.Column("commons_license", sa.String(length=32), nullable=True))
    op.add_column("dive_site", sa.Column("commons_seed_registry", sa.String(length=255), nullable=True))
    op.add_column("dive_site", sa.Column("commons_seed_identifier", sa.String(length=255), nullable=True))
    op.add_column("dive_site", sa.Column("commons_retrieved", sa.Date(), nullable=True))
    op.create_check_constraint("ck_dive_site_owner_provenance", "dive_site", _OWNER_PROVENANCE)


def downgrade() -> None:
    op.execute("DELETE FROM dive_site WHERE user_id IS NULL")
    op.drop_constraint("ck_dive_site_owner_provenance", "dive_site", type_="check")
    op.drop_column("dive_site", "commons_retrieved")
    op.drop_column("dive_site", "commons_seed_identifier")
    op.drop_column("dive_site", "commons_seed_registry")
    op.drop_column("dive_site", "commons_license")
    op.drop_column("dive_site", "commons_source")
    op.alter_column("dive_site", "user_id", existing_type=sa.Integer(), nullable=False)

    op.add_column("dive_site", sa.Column("notes", sa.Text(), nullable=False, server_default=""))
    op.execute(
        "UPDATE dive_site SET notes = saved_site.notes FROM saved_site "
        "WHERE saved_site.dive_site_id = dive_site.id AND saved_site.user_id = dive_site.user_id"
    )
