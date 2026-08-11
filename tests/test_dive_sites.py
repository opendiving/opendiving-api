"""Unit tests for the dive site list/search surface (`api/v1/dive_sites.py`,
`core/utils/owned_resource_cache.py`).

Like the other suites here these cover the pure-logic pieces - the `LIKE` escaping, the
shape of the search `WHERE` clause, and the cache key - rather than the endpoint on top
of a live Postgres/Redis, which is exercised by hand (see DECISIONS.md).
"""

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from src.app.api.v1.dive_sites import MAX_DIVE_SITES_PER_PAGE, _dive_site_cache
from src.app.api.v1.trips import _trip_cache
from src.app.core.utils.owned_resource_cache import escape_like
from src.app.models.dive_site import DiveSite


def _compiled_search_sql(term: str, user_id: int = 1) -> str:
    """The `WHERE` clause the search would actually run, as literal Postgres SQL."""
    conditions = _dive_site_cache.search_conditions(user_id=user_id, term=term)
    statement = select(DiveSite.id).where(*conditions)
    return str(
        statement.compile(
            # `named` keeps literal `%` out of the printf-style escaping the default
            # `pyformat` paramstyle applies, which would double every one of them.
            dialect=postgresql.dialect(paramstyle="named"),
            compile_kwargs={"literal_binds": True},
        )
    )


class TestEscapeLike:
    def test_leaves_an_ordinary_term_alone(self) -> None:
        assert escape_like("blue hole") == "blue hole"

    @pytest.mark.parametrize(
        ("term", "expected"),
        [
            ("50%", "50\\%"),
            ("blue_hole", "blue\\_hole"),
            ("%", "\\%"),
        ],
    )
    def test_escapes_wildcards_so_they_match_literally(self, term: str, expected: str) -> None:
        assert escape_like(term) == expected

    def test_escapes_the_escape_character_itself_first(self) -> None:
        # Escaping "%" before "\" would turn "\" + "%" into "\\%" - a literal backslash
        # followed by a *live* wildcard.
        assert escape_like("\\%") == "\\\\\\%"


class TestDiveSiteSearchConditions:
    def test_matches_the_term_against_either_name_or_location(self) -> None:
        sql = _compiled_search_sql("dahab")

        # The two column matches are OR'd *inside their own group*, so the user/is_deleted
        # scoping still applies to both - a site named "Blue Hole" in Dahab has to match a
        # search for either word, without the OR leaking past the ownership check.
        assert (
            "(dive_site.name ILIKE '%dahab%' ESCAPE '\\\\' OR dive_site.location ILIKE '%dahab%' ESCAPE '\\\\')" in sql
        )

    def test_stays_scoped_to_the_users_own_non_deleted_sites(self) -> None:
        sql = _compiled_search_sql("dahab", user_id=42)

        assert "dive_site.user_id = 42" in sql
        assert "dive_site.is_deleted IS false" in sql

    def test_declares_the_escape_character_alongside_the_pattern(self) -> None:
        sql = _compiled_search_sql("50%")

        # Without the ESCAPE clause the backslash `escape_like` added would be matched
        # literally instead of neutralizing the "%". (Backslashes render doubled in
        # literal SQL; the real query sends the pattern as a bound parameter.)
        assert "ILIKE '%50\\\\%%' ESCAPE '\\\\'" in sql


class TestDiveSiteListCacheKey:
    def test_the_search_term_is_part_of_the_key(self) -> None:
        # Two different searches on the same page must not serve each other's results.
        assert ":search:{search}" in _dive_site_cache.list_cache_key_prefix

    def test_page_and_page_size_are_still_part_of_the_key(self) -> None:
        prefix = _dive_site_cache.list_cache_key_prefix

        assert "page_{page}" in prefix
        assert "items_per_page:{items_per_page}" in prefix

    def test_the_key_stays_under_the_users_invalidation_wildcard(self) -> None:
        # `invalidate_list` purges `user_{id}_dive_sites:*`, so the search segment has to sit
        # after that prefix - otherwise a rename would leave stale search results cached.
        assert _dive_site_cache.list_cache_key_prefix.startswith("user_{user_id}_dive_sites:")

    def test_a_resource_without_search_keeps_the_original_key_shape(self) -> None:
        # `read_list` is called without a `search` kwarg for those, and `@cache` would
        # `KeyError` on a placeholder it can't fill.
        assert ":search:" not in _trip_cache.list_cache_key_prefix


class TestPageSizeCap:
    def test_the_cap_is_low_enough_to_stop_a_whole_table_fetch(self) -> None:
        assert MAX_DIVE_SITES_PER_PAGE == 100


def test_the_model_columns_the_search_reads_actually_exist() -> None:
    for column in ("name", "location"):
        assert hasattr(DiveSite, column)
