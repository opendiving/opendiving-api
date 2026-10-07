"""`GET /user/species/suggest`: the caller's own species ahead of the catalog search.

Postgres-backed for the ordering, because the three tiers are one `ORDER BY` over a filtered
aggregate and a mocked session cannot be wrong about it. The register half is stubbed at
`_remote_search`, the one function in the search that leaves the instance.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api import router as api_router
from src.app.api.dependencies import get_current_user
from src.app.api.v1 import species as species_routes
from src.app.api.v1 import users as users_module
from src.app.api.v1.users import read_species_suggestions
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.core.setup import create_application
from src.app.models.dive_dive_site import DiveDiveSite
from src.app.models.dive_species import DiveSpecies
from src.app.models.species_name import SpeciesName
from src.app.schemas.species import SpeciesSearchResponse, SpeciesSearchResult, SpeciesSuggestResponse
from src.app.services import species_service
from src.app.services.species_life_list import suggest_species
from tests.conftest import db_available
from tests.helpers.generators import create_dive, create_dive_site, create_species, create_user

SUGGEST_PATH = "/api/v1/user/species/suggest"
PAGE = species_service._MAX_RESULTS
CURRENT_USER = {"id": 7, "uuid": uuid7(), "username": "ada", "is_superuser": False}


@pytest.fixture(scope="module")
def suggest_app() -> Any:
    return create_application(router=api_router, settings=settings, apply_migrations_on_start=False)


@pytest.fixture
def client(suggest_app: Any) -> Iterator[TestClient]:
    suggest_app.dependency_overrides[get_current_user] = lambda: CURRENT_USER
    suggest_app.dependency_overrides[async_get_db] = lambda: MagicMock()
    with TestClient(suggest_app) as test_client:
        yield test_client
    suggest_app.dependency_overrides = {}


class TestTheRoute:
    def test_it_is_not_read_as_a_species_uuid(self, client: TestClient) -> None:
        """Declared before `/user/species/{uuid}`, which would otherwise 422 on "suggest"."""
        with patch.object(users_module, "suggest_species", AsyncMock(return_value=SpeciesSuggestResponse())) as service:
            response = client.get(SUGGEST_PATH)

        assert response.status_code == 200
        assert response.json() == {"results": [], "has_more": False}
        service.assert_awaited_once()

    def test_it_passes_the_query_and_the_excluded_species_through(self, client: TestClient) -> None:
        held = [uuid7(), uuid7()]
        with patch.object(users_module, "suggest_species", AsyncMock(return_value=SpeciesSuggestResponse())) as service:
            client.get(SUGGEST_PATH, params={"q": "wra", "exclude_species_uuid": [str(u) for u in held]})

        assert service.await_args is not None
        kwargs = service.await_args.kwargs
        assert kwargs["query"] == "wra"
        assert list(kwargs["excluded_uuids"]) == held
        assert kwargs["dive_site_ids"] == []

    @pytest.mark.parametrize("parameter", ["dive_site_uuid", "exclude_species_uuid"])
    def test_more_uuids_than_the_cap_is_a_422(self, client: TestClient, parameter: str) -> None:
        response = client.get(SUGGEST_PATH, params={parameter: [str(uuid7()) for _ in range(101)]})

        assert response.status_code == 422

    def test_a_query_over_the_column_width_is_a_422(self, client: TestClient) -> None:
        assert client.get(SUGGEST_PATH, params={"q": "x" * 256}).status_code == 422


def _log(
    db: Session,
    diver: Any,
    species: list[Any],
    *,
    day: int,
    sites: tuple[Any, ...] = (),
    is_deleted: bool = False,
) -> Any:
    dive = create_dive(db, diver, is_deleted=is_deleted)
    dive.start_time = datetime(2026, 6, day, 9, 0, tzinfo=UTC)
    for position, one in enumerate(species):
        db.add(DiveSpecies(dive_id=dive.id, species_id=one.id, position=position))
    for position, site in enumerate(sites):
        db.add(DiveDiveSite(dive_id=dive.id, dive_site_id=site.id, position=position))
    db.commit()
    return dive


def _remote(*results: SpeciesSearchResult, has_more: bool = False) -> Any:
    answer = SpeciesSearchResponse(results=list(results), has_more=has_more)
    return patch.object(species_service, "_remote_search", AsyncMock(return_value=answer))


def _never_remote() -> Any:
    return patch.object(species_service, "_remote_search", AsyncMock(side_effect=AssertionError("registers asked")))


def _remote_row(aphia_id: int, name: str, *, matched_name: str | None = None) -> SpeciesSearchResult:
    return SpeciesSearchResult(
        aphia_id=aphia_id,
        scientific_name=name,
        rank="Species",
        status="accepted",
        matched_name=matched_name,
        source="worms",
        attribution="WoRMS",
    )


async def _suggest(
    async_db: AsyncSession,
    diver: Any,
    query: str = "",
    *,
    sites: tuple[Any, ...] = (),
    excluded: tuple[Any, ...] = (),
    before_search: AsyncMock | None = None,
) -> SpeciesSuggestResponse:
    return await suggest_species(
        async_db,
        user_id=diver.id,
        query=query,
        dive_site_ids=[site.id for site in sites],
        excluded_uuids=[one.uuid for one in excluded],
        before_search=before_search or AsyncMock(),
    )


def _names(page: SpeciesSuggestResponse) -> list[str]:
    return [row.scientific_name for row in page.results]


@pytest.mark.skipif(not db_available(), reason="requires a database")
class TestTheTiers:
    def _seed(self, db: Session) -> tuple[Any, Any, Any, Any, Any, Any]:
        """Two sites and three species: `first` on two dives at `north`, `second` once at
        `south` and once elsewhere, `third` on three dives at neither - plus a deleted dive
        and another diver's dive that would each move the order if they counted."""
        diver, stranger = create_user(db), create_user(db)
        north, south = create_dive_site(db, diver), create_dive_site(db, diver)
        first, second, third = create_species(db), create_species(db), create_species(db)
        _log(db, diver, [first], day=1, sites=(north,))
        _log(db, diver, [first], day=2, sites=(north,))
        _log(db, diver, [second], day=3, sites=(south,))
        _log(db, diver, [second], day=4)
        for day in (5, 6, 7):
            _log(db, diver, [third], day=day)
        for _ in range(4):
            _log(db, diver, [third, second], day=8, sites=(north,), is_deleted=True)
            _log(db, stranger, [third, second], day=8, sites=(north,))
        return diver, north, south, first, second, third

    @pytest.mark.asyncio
    async def test_one_site_puts_its_species_first_by_dives_there(self, db: Session, async_db: AsyncSession) -> None:
        diver, north, _, first, second, third = self._seed(db)

        page = await _suggest(async_db, diver, sites=(north,))

        assert _names(page) == [first.scientific_name, third.scientific_name, second.scientific_name]
        assert [row.dive_count_at_sites for row in page.results] == [2, 0, 0]
        assert page.has_more is False

    @pytest.mark.asyncio
    async def test_two_sites_are_one_tier_ordered_by_count(self, db: Session, async_db: AsyncSession) -> None:
        diver, north, south, first, second, third = self._seed(db)

        page = await _suggest(async_db, diver, sites=(north, south))

        assert _names(page) == [first.scientific_name, second.scientific_name, third.scientific_name]
        assert [row.dive_count_at_sites for row in page.results] == [2, 1, 0]

    @pytest.mark.asyncio
    async def test_no_site_is_the_life_list_by_last_seen(self, db: Session, async_db: AsyncSession) -> None:
        diver, _, _, first, second, third = self._seed(db)

        page = await _suggest(async_db, diver)

        assert _names(page) == [third.scientific_name, second.scientific_name, first.scientific_name]
        assert all(row.dive_count_at_sites == 0 for row in page.results)
        assert [row.last_seen for row in page.results] == [
            datetime(2026, 6, day, 9, 0, tzinfo=UTC) for day in (7, 4, 2)
        ]

    @pytest.mark.asyncio
    async def test_a_dive_naming_two_of_the_sites_counts_once(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        north, south = create_dive_site(db, diver), create_dive_site(db, diver)
        drift = create_species(db)
        _log(db, diver, [drift], day=1, sites=(north, south))

        page = await _suggest(async_db, diver, sites=(north, south))

        assert page.results[0].dive_count_at_sites == 1

    @pytest.mark.asyncio
    async def test_a_tie_at_the_site_goes_to_the_one_seen_last_then_to_the_catalog_order(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        diver = create_user(db)
        site = create_dive_site(db, diver)
        older, newer, a, b = (create_species(db) for _ in range(4))
        _log(db, diver, [older], day=1, sites=(site,))
        _log(db, diver, [newer], day=2, sites=(site,))
        # One dive, so the same last-seen instant: the catalog id keeps the order stable.
        _log(db, diver, [b, a], day=3, sites=(site,))

        page = await _suggest(async_db, diver, sites=(site,))

        assert _names(page) == [a.scientific_name, b.scientific_name, newer.scientific_name, older.scientific_name]

    @pytest.mark.asyncio
    async def test_a_species_only_on_deleted_or_foreign_dives_is_in_no_own_tier(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        diver, stranger = create_user(db), create_user(db)
        gone, theirs = create_species(db), create_species(db)
        _log(db, diver, [gone], day=1, is_deleted=True)
        _log(db, stranger, [theirs], day=1)

        assert (await _suggest(async_db, diver)).results == []


@pytest.mark.skipif(not db_available(), reason="requires a database")
class TestTheRow:
    @pytest.mark.asyncio
    async def test_an_own_row_is_a_catalog_row_with_the_divers_history(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        diver = create_user(db)
        site = create_dive_site(db, diver)
        species = create_species(db, common_name="zzfixture row")
        dive = _log(db, diver, [species], day=9, sites=(site,))
        dive.utc_offset_minutes = 420
        db.commit()

        row = (await _suggest(async_db, diver, sites=(site,))).results[0]

        assert set(row.model_dump()) == {
            *SpeciesSearchResult.model_fields,
            "dive_count_at_sites",
            "last_seen",
        }
        assert row.uuid == species.uuid
        assert row.source == "catalog"
        assert row.attribution == species_service._WORMS_ATTRIBUTION
        assert row.matched_name is None
        assert row.dive_count_at_sites == 1
        # The offset the dive was logged in, as the life list reports it.
        assert row.last_seen == datetime(2026, 6, 9, 9, 0, tzinfo=UTC)
        assert isinstance(row.last_seen, datetime)
        assert row.last_seen.hour == 16

    @pytest.mark.asyncio
    async def test_a_search_row_carries_no_history(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        token = f"zzfixture{uuid7().hex[-10:]}"

        with _remote(_remote_row(int(uuid7().hex[-7:], 16), f"{token} remote")):
            row = (await _suggest(async_db, diver, token)).results[0]

        assert set(row.model_dump()) == {*SpeciesSearchResult.model_fields, "dive_count_at_sites", "last_seen"}
        assert row.source == "worms"
        assert row.dive_count_at_sites == 0
        assert row.last_seen is None


@pytest.mark.skipif(not db_available(), reason="requires a database")
class TestTheQuery:
    @pytest.mark.asyncio
    async def test_it_matches_the_common_and_the_scientific_name(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        token = uuid7().hex[-10:]
        by_common = create_species(db, common_name=f"zzfixture {token} wrasse")
        by_scientific = create_species(db, scientific_name=f"zzfixture-{token}-labroides")
        create_species(db)
        _log(db, diver, [by_common], day=1)
        _log(db, diver, [by_scientific], day=2)

        with _remote():
            wrasse = await _suggest(async_db, diver, f"{token} WRASSE")
            labroides = await _suggest(async_db, diver, f"{token}-labroides")

        assert [row.uuid for row in wrasse.results if row.last_seen] == [by_common.uuid]
        assert [row.uuid for row in labroides.results if row.last_seen] == [by_scientific.uuid]
        assert all(row.matched_name is None for row in wrasse.results + labroides.results if row.last_seen)

    @pytest.mark.asyncio
    async def test_an_alias_alone_reaches_the_species_through_the_search_tier(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """Visible names only: a stored alias does not place a species in an own tier, but the
        catalog search still finds it and says why."""
        diver = create_user(db)
        token = uuid7().hex[-10:]
        species = create_species(db)
        alias = f"zzfixture {token} lippfisch"
        db.add(SpeciesName(species_id=species.id, name=alias, kind="common", source="worms", language_code="deu"))
        _log(db, diver, [species], day=1)

        with _remote():
            page = await _suggest(async_db, diver, token)

        assert [(row.uuid, row.last_seen, row.matched_name) for row in page.results] == [(species.uuid, None, alias)]

    @pytest.mark.asyncio
    async def test_a_wildcard_is_escaped(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        literal = create_species(db, common_name="zzfixture 100% coral")
        decoy = create_species(db, common_name="zzfixture 100 and then coral")
        _log(db, diver, [literal, decoy], day=1)

        with _remote():
            page = await _suggest(async_db, diver, "100% coral")

        assert [row.uuid for row in page.results if row.last_seen] == [literal.uuid]

    @pytest.mark.asyncio
    async def test_a_species_found_both_ways_is_shown_once_in_the_own_tier(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        diver = create_user(db)
        token = f"zzfixture{uuid7().hex[-10:]}"
        species = create_species(db, common_name=f"{token} anthias")
        _log(db, diver, [species], day=1)

        with _remote(_remote_row(species.aphia_id, species.scientific_name)):
            page = await _suggest(async_db, diver, token)

        assert [(row.aphia_id, row.last_seen is not None) for row in page.results] == [(species.aphia_id, True)]


@pytest.mark.skipif(not db_available(), reason="requires a database")
class TestWhenTheSearchIsAsked:
    def _diver_with(self, db: Session, count: int) -> Any:
        diver = create_user(db)
        for _ in range(count):
            _log(db, diver, [create_species(db)], day=1)
        return diver

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", ["", "z"])
    async def test_not_for_an_empty_or_one_letter_query(self, db: Session, async_db: AsyncSession, query: str) -> None:
        diver = self._diver_with(db, 2)
        paid = AsyncMock()

        with _never_remote():
            page = await _suggest(async_db, diver, query, before_search=paid)

        assert len(page.results) == 2
        paid.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_not_when_the_own_matches_fill_the_page(self, db: Session, async_db: AsyncSession) -> None:
        diver = self._diver_with(db, PAGE)
        paid = AsyncMock()

        with _never_remote():
            page = await _suggest(async_db, diver, "zz", before_search=paid)

        assert len(page.results) == PAGE
        assert page.has_more is True
        paid.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_with_two_letters_and_room_on_the_page_and_paid_for_first(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        diver = self._diver_with(db, 1)
        order: list[str] = []
        paid = AsyncMock(side_effect=lambda: order.append("paid"))

        async def remote(query: str) -> SpeciesSearchResponse:
            order.append("asked")
            return SpeciesSearchResponse()

        with patch.object(species_service, "_remote_search", remote):
            await _suggest(async_db, diver, "zz", before_search=paid)

        assert order == ["paid", "asked"]


@pytest.mark.skipif(not db_available(), reason="requires a database")
class TestHasMore:
    @pytest.mark.asyncio
    async def test_false_when_everything_fit(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        _log(db, diver, [create_species(db)], day=1)

        assert (await _suggest(async_db, diver)).has_more is False

    @pytest.mark.asyncio
    async def test_true_when_the_own_rows_were_cut(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        for _ in range(PAGE + 1):
            _log(db, diver, [create_species(db)], day=1)

        page = await _suggest(async_db, diver)

        assert len(page.results) == PAGE
        assert page.has_more is True

    @pytest.mark.asyncio
    async def test_true_for_one_letter_whose_search_was_skipped(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        _log(db, diver, [create_species(db)], day=1)

        with _never_remote():
            assert (await _suggest(async_db, diver, "z")).has_more is True

    @pytest.mark.asyncio
    async def test_true_when_the_search_said_so(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        token = f"zzfixture{uuid7().hex[-10:]}"

        with _remote(_remote_row(int(uuid7().hex[-7:], 16), f"{token} a"), has_more=True):
            assert (await _suggest(async_db, diver, token)).has_more is True

    @pytest.mark.asyncio
    async def test_true_when_the_appended_rows_were_cut(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        token = f"zzfixture{uuid7().hex[-10:]}"
        _log(db, diver, [create_species(db, common_name=f"{token} own")], day=1)
        remote = [_remote_row(int(uuid7().hex[-7:], 16), f"{token} {n:02}") for n in range(PAGE)]

        with _remote(*remote):
            page = await _suggest(async_db, diver, token)

        assert len(page.results) == PAGE
        assert page.results[0].last_seen is not None
        assert page.has_more is True


@pytest.mark.skipif(not db_available(), reason="requires a database")
class TestTheExclusion:
    @pytest.mark.asyncio
    async def test_the_page_refills_past_an_excluded_species(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        site = create_dive_site(db, diver)
        top, runner_up = create_species(db), create_species(db)
        _log(db, diver, [top], day=1, sites=(site,))
        _log(db, diver, [top], day=2, sites=(site,))
        _log(db, diver, [runner_up], day=3, sites=(site,))
        for _ in range(PAGE):
            _log(db, diver, [create_species(db)], day=4)

        page = await _suggest(async_db, diver, sites=(site,), excluded=(top,))

        assert page.results[0].uuid == runner_up.uuid
        assert top.uuid not in {row.uuid for row in page.results}
        assert len(page.results) == PAGE

    @pytest.mark.asyncio
    async def test_an_excluded_species_is_dropped_from_the_search_rows_too(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """The common case: picked from the catalog on this form, never logged before."""
        diver = create_user(db)
        token = f"zzfixture{uuid7().hex[-10:]}"
        held = create_species(db, scientific_name=f"{token} held")
        other = create_species(db, scientific_name=f"{token} other")

        with _remote():
            page = await _suggest(async_db, diver, token, excluded=(held,))

        assert [row.uuid for row in page.results] == [other.uuid]

    @pytest.mark.asyncio
    async def test_an_unknown_uuid_changes_nothing(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        species = create_species(db)
        _log(db, diver, [species], day=1)

        page = await suggest_species(
            async_db,
            user_id=diver.id,
            query="",
            dive_site_ids=[],
            excluded_uuids=[uuid7()],
            before_search=AsyncMock(),
        )

        assert [row.uuid for row in page.results] == [species.uuid]


@pytest.mark.skipif(not db_available(), reason="requires a database")
class TestTheRouteAgainstPostgres:
    """The route's own work: resolving the sites to the caller's, and paying the budget."""

    async def _call(self, async_db: AsyncSession, diver: Any, q: str = "", sites: list[Any] | None = None) -> Any:
        return await read_species_suggestions(
            current_user={"id": diver.id},
            db=async_db,
            q=q,
            dive_site_uuid=sites,
            exclude_species_uuid=None,
        )

    @pytest.mark.asyncio
    async def test_a_foreign_site_beside_an_own_one_is_ignored(self, db: Session, async_db: AsyncSession) -> None:
        diver, stranger = create_user(db), create_user(db)
        mine, theirs = create_dive_site(db, diver), create_dive_site(db, stranger)
        here, elsewhere = create_species(db), create_species(db)
        _log(db, diver, [here], day=1, sites=(mine,))
        _log(db, diver, [elsewhere], day=2)

        alone = await self._call(async_db, diver, sites=[mine.uuid])
        with_foreign = await self._call(async_db, diver, sites=[mine.uuid, theirs.uuid, uuid7()])
        foreign_only = await self._call(async_db, diver, sites=[theirs.uuid])

        assert with_foreign == alone
        assert alone.results[0].uuid == here.uuid
        assert all(row.dive_count_at_sites == 0 for row in foreign_only.results)

    @pytest.mark.asyncio
    async def test_the_budget_is_spent_only_when_the_registers_are_asked(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        diver = create_user(db)
        for _ in range(PAGE):
            _log(db, diver, [create_species(db, common_name=f"zzfixture full {uuid7().hex[-8:]}")], day=1)
        roomy = create_user(db)
        _log(db, roomy, [create_species(db)], day=1)

        with patch.object(species_routes, "enforce_rate_limit", AsyncMock()) as limit, _never_remote():
            await self._call(async_db, diver)
            await self._call(async_db, diver, "z")
            await self._call(async_db, diver, "zz")
            limit.assert_not_awaited()

        with patch.object(species_routes, "enforce_rate_limit", AsyncMock()) as limit, _remote():
            await self._call(async_db, roomy, "zz")

        limit.assert_awaited_once_with(
            f"species:user:{roomy.id}", settings.SPECIES_RATE_LIMIT_PER_USER, settings.SPECIES_RATE_LIMIT_WINDOW_SECONDS
        )
