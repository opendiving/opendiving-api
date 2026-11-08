"""Dive sites are searched by trigram and by position

Revision ID: 65940d01a99c
Revises: 04c319ff9731
Create Date: 2026-11-08 12:00:00.000000

`pg_trgm` and `unaccent`, two functions that fold a site's name and other names into one
case- and accent-free text, a trigram index over that text, and a btree over the pin. Both
extensions are trusted, so the database's owner creates them without a superuser. Nothing is
backfilled: both indexes are built from the rows as they stand.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "65940d01a99c"
down_revision: str | None = "04c319ff9731"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every name the bodies reach is schema-qualified: `CREATE INDEX` evaluates them with
# `search_path` set to `pg_catalog, pg_temp`, and a plain-SQL dump restores with it empty.
# `unaccent` is `STABLE` as shipped, and an index expression must be `IMMUTABLE`; naming the
# dictionary pins the one input that could make it vary.
_FOLD = """
CREATE OR REPLACE FUNCTION public.opendiving_fold(value text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $$ SELECT pg_catalog.lower(public.unaccent('public.unaccent'::regdictionary, value)) $$
"""

# The name, then each other name, each between unit separators, so a pattern can anchor on a
# whole name or its start, and no match runs from one name into the next.
_SEARCH_TEXT = """
CREATE OR REPLACE FUNCTION public.dive_site_search_text(name text, other_names json) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
SELECT E'\\x1f' || coalesce(public.opendiving_fold(name), '') || E'\\x1f'
    || coalesce(
        (SELECT pg_catalog.string_agg(public.opendiving_fold(value) || E'\\x1f', '')
         FROM pg_catalog.json_array_elements_text(other_names) AS value),
        ''
    )
$$
"""


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA public")
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent SCHEMA public")
    op.execute(_FOLD)
    op.execute(_SEARCH_TEXT)
    op.create_index(
        "ix_dive_site_search_text_trgm",
        "dive_site",
        [sa.literal_column("public.dive_site_search_text(name, other_names) gin_trgm_ops")],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index("ix_dive_site_latitude_longitude", "dive_site", ["latitude", "longitude"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_dive_site_latitude_longitude", table_name="dive_site")
    op.drop_index("ix_dive_site_search_text_trgm", table_name="dive_site")
    op.execute("DROP FUNCTION public.dive_site_search_text(text, json)")
    op.execute("DROP FUNCTION public.opendiving_fold(text)")
    # The extensions stay: an operator may have created them by hand before this revision ran,
    # and dropping one takes whatever else of theirs depends on it.
