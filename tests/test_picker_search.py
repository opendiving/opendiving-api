"""Unit tests for the `search=` surface behind the dive form's pickers
(`api/v1/dive_sites.py`, `api/v1/trips.py`, `api/v1/courses.py`, `api/v1/gear_items.py`,
`core/utils/search.py`, `core/utils/owned_resource_cache.py`).

Like the other suites here these cover the pure-logic pieces - the `LIKE` escaping, the
shape of the search `WHERE` clause, and the cache keys - rather than the endpoints on
top of a live Postgres/Redis, which are exercised by hand (see DECISIONS.md).
"""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import ColumnElement, select
from sqlalchemy.dialects import postgresql

from src.app.api.v1.courses import _course_cache
from src.app.api.v1.dive_sites import _dive_site_cache
from src.app.api.v1.gear_items import GEAR_ITEM_SEARCH_COLUMNS
from src.app.api.v1.trips import _trip_cache
from src.app.core.utils.owned_resource_cache import OwnedResourceCache
from src.app.core.utils.pagination import DEFAULT_MAX_ITEMS_PER_PAGE, clamp_pagination
from src.app.core.utils.search import escape_like, search_clause
from src.app.crud.crud_courses import COURSE_SEARCH_COLUMNS
from src.app.crud.crud_gear_items import crud_gear_items
from src.app.models.course import Course
from src.app.models.dive_site import DiveSite
from src.app.models.gear_item import GearItem
from src.app.models.trip import Trip


def _as_sql(*conditions: ColumnElement[bool], model: Any) -> str:
    """The given `WHERE` clauses as literal Postgres SQL, for asserting on their shape."""
    statement = select(model.id).where(*conditions)
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


class TestSearchClause:
    @pytest.mark.parametrize(
        ("model", "columns", "table"),
        [
            (DiveSite, ("name", "location"), "dive_site"),
            (GearItem, GEAR_ITEM_SEARCH_COLUMNS, "gear_item"),
        ],
    )
    def test_matches_the_term_against_any_of_its_columns(
        self, model: Any, columns: tuple[str, ...], table: str
    ) -> None:
        sql = _as_sql(search_clause(model, columns, "dahab"), model=model)

        # OR'd, not AND'd: a site called "Dahab Canyon" and one merely *located* in Dahab
        # both have to match, and neither has the term in both columns. Trips are not here
        # any more - they search one column and an EXISTS over `trip_location`, which is
        # `trips.py::_search_conditions` rather than `search_clause`.
        #
        # Courses are absent for a third reason again: they *do* go through `search_clause`,
        # but over one column, so there is no OR for this to find. Only the multi-column
        # resources belong here; every searchable model is checked for the columns actually
        # existing by the module-level test at the bottom of this file.
        assert " OR " in sql
        for column in columns:
            assert f"{table}.{column} ILIKE '%dahab%'" in sql

    def test_declares_the_escape_character_alongside_the_pattern(self) -> None:
        sql = _as_sql(search_clause(DiveSite, ("name",), "50%"), model=DiveSite)

        # Without the ESCAPE clause the backslash `escape_like` added would be matched
        # literally instead of neutralizing the "%". (Backslashes render doubled in
        # literal SQL; the real query sends the pattern as a bound parameter.)
        assert "ILIKE '%50\\\\%%' ESCAPE '\\\\'" in sql


class TestOwnedResourceSearchConditions:
    def test_the_column_matches_are_grouped_inside_the_ownership_scoping(self) -> None:
        sql = _as_sql(*_dive_site_cache.search_conditions(user_id=42, term="dahab"), model=DiveSite)

        # The OR has to stay parenthesized, or it would leak past the ownership check and
        # match every user's sites.
        assert (
            "dive_site.user_id = 42 "
            "AND (dive_site.name ILIKE '%dahab%' ESCAPE '\\\\' "
            "OR dive_site.location ILIKE '%dahab%' ESCAPE '\\\\')" in sql
        )

    def test_trips_are_scoped_the_same_way(self) -> None:
        sql = _as_sql(*_trip_cache.search_conditions(user_id=7, term="dahab"), model=Trip)

        assert "trip.user_id = 7" in sql
        # The ownership scope is the only one left - both models are hard-deleted, so
        # there is no liveness clause for the OR to leak past.
        assert "is_deleted" not in sql


class TestListCacheKeys:
    @pytest.mark.parametrize("resource", [_dive_site_cache, _trip_cache, _course_cache])
    def test_the_search_term_is_part_of_the_key(self, resource: OwnedResourceCache) -> None:
        # Two different searches on the same page must not serve each other's results.
        assert ":search:{search}" in resource.list_cache_key_prefix

    @pytest.mark.parametrize("resource", [_dive_site_cache, _trip_cache, _course_cache])
    def test_page_and_page_size_are_still_part_of_the_key(self, resource: OwnedResourceCache) -> None:
        prefix = resource.list_cache_key_prefix

        assert "page_{page}" in prefix
        assert "items_per_page:{items_per_page}" in prefix

    @pytest.mark.parametrize(
        ("resource", "expected_prefix"),
        [
            (_dive_site_cache, "user_{user_id}_dive_sites:"),
            (_trip_cache, "user_{user_id}_trips:"),
            (_course_cache, "user_{user_id}_courses:"),
        ],
    )
    def test_the_key_stays_under_the_users_invalidation_wildcard(
        self, resource: OwnedResourceCache, expected_prefix: str
    ) -> None:
        # `invalidate_list` purges `user_{id}_{resource}:*`, so the search segment has to
        # sit after that prefix - otherwise a rename would leave stale results cached.
        assert resource.list_cache_key_prefix.startswith(expected_prefix)

    def test_a_resource_without_search_keeps_the_original_key_shape(self) -> None:
        # `read_list` is called without a `search` kwarg for those, and `@cache` would
        # `KeyError` on a placeholder it can't fill.
        unsearchable: OwnedResourceCache = OwnedResourceCache(
            resource_name="gear_sets",
            resource_label="Gear set",
            item_cache_prefix="gear_set_cache",
            crud=crud_gear_items,
            schema_to_select=dict,
            to_public=lambda item, user_uuid: item,
            sort_columns="name",
        )

        assert ":search:" not in unsearchable.list_cache_key_prefix


class TestPageSizeCaps:
    def test_the_cap_is_low_enough_to_matter(self) -> None:
        # Low enough that no single request can pull a whole table, which is exactly what
        # the pickers used to do in a loop.
        assert DEFAULT_MAX_ITEMS_PER_PAGE == 100

    @pytest.mark.parametrize("requested", [101, 1_000, 999_999_999])
    def test_an_oversized_page_is_capped(self, requested: int) -> None:
        _, items_per_page = clamp_pagination(1, requested)
        assert items_per_page == DEFAULT_MAX_ITEMS_PER_PAGE

    @pytest.mark.parametrize(("page", "items_per_page"), [(0, 0), (-1, -50)])
    def test_non_positive_values_are_floored_to_one(self, page: int, items_per_page: int) -> None:
        # A negative `items_per_page` would otherwise reach the database as a negative
        # LIMIT, and a negative `page` as a negative OFFSET.
        assert clamp_pagination(page, items_per_page) == (1, 1)

    def test_a_reasonable_request_is_left_alone(self) -> None:
        assert clamp_pagination(3, 25) == (3, 25)

    def test_every_list_endpoint_clamps(self) -> None:
        """The bug this replaces: three of eight list endpoints clamped and five didn't.
        (Eight then; the dict below has grown since, and the historical count stays as it
        was.)

        Asserting on the source keeps that from silently regressing when a new list
        endpoint is added by copying one of the five that used to be unbounded.
        """
        list_routes = {
            "certifications.py": "read_certifications",
            "courses.py": "read_courses",
            "dive_sites.py": "read_dive_sites",
            "dives.py": "read_dives",
            "gear_items.py": "read_gear_items",
            "gear_service.py": "read_gear_service_schedules",
            "gear_sets.py": "read_gear_sets",
            "trips.py": "read_trips",
        }
        routes_dir = Path(__file__).resolve().parents[1] / "src" / "app" / "api" / "v1"

        for filename, function_name in list_routes.items():
            source = (routes_dir / filename).read_text()
            assert f"def {function_name}(" in source, f"{filename} no longer defines {function_name}"
            assert "clamp_pagination(page, items_per_page)" in source, f"{filename} does not clamp pagination"


@pytest.mark.parametrize(
    ("model", "columns"),
    [
        (DiveSite, ("name", "location")),
        (Trip, ("name",)),
        (Course, COURSE_SEARCH_COLUMNS),
        (GearItem, GEAR_ITEM_SEARCH_COLUMNS),
    ],
)
def test_the_model_columns_the_search_reads_actually_exist(model: Any, columns: tuple[str, ...]) -> None:
    for column in columns:
        assert hasattr(model, column)
