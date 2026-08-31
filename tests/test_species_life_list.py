"""The life list (`GET /user/species`) and the dive filter beside it (`GET /dives?species_uuid=`).

Two classes here would not have caught anything by existing, and two would.

**`TestTheCacheKeyCarriesTheWholeQuery` is the one this feature's history is about.** `@cache`
builds its key from the `key_prefix` placeholders and the resource id and nothing else, so a
prefix missing `page` or `search` collapses every page *and* every search of one diver onto a
single entry - page 2 serving page 1, a search serving the unfiltered list, for sixty seconds
at a time. Nothing downstream catches it: `TestListCacheKeys` in `test_picker_search.py` asserts
`page_{page}` only for `OwnedResourceCache` instances, and this is deliberately a hand-written
aggregate. It is also invisible to any test run outside the TTL, which is precisely how it would
ship.

**`TestTheAggregate` is the other.** `dive_species` carries no liveness of its own and its
`dive_id` cascade is dormant - dives are soft-deleted, so no `DELETE FROM dive` ever fires and
the join rows of a deleted dive survive it. Forgetting to reach through to `Dive.is_deleted`
shows a diver dives they deleted, quietly. It is Postgres-backed because the correctness lives
in the SQL: a `GROUP BY` leaning on functional-dependency inference, three aggregates and an
`EXISTS` are not things a mocked session can be wrong about.
"""

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api import router as api_router
from src.app.api.dependencies import get_current_user
from src.app.api.v1 import users as users_module
from src.app.api.v1.users import SPECIES_LIFE_LIST_CACHE_KEY_PREFIX
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.core.setup import create_application
from src.app.models.dive_species import DiveSpecies
from src.app.services.dive_stats import recalculate_dive_stats
from src.app.services.species_life_list import species_life_list
from tests.conftest import db_available
from tests.helpers.generators import create_dive, create_species, create_user
from tests.helpers.routes import iter_api_routes

CURRENT_USER = {"id": 7, "uuid": uuid7(), "username": "ada", "is_superuser": False}

LIFE_LIST_PATH = "/api/v1/user/species"


@pytest.fixture(scope="module")
def life_list_app() -> Any:
    return create_application(router=api_router, settings=settings, apply_migrations_on_start=False)


class _FakeRedis:
    """Enough of the client for `@cache` to run. The decorator raises `MissingClientError`
    when there is none, so these route tests cannot simply have Redis absent - and the app's
    lifespan builds a real pool pointed at the compose hostname, which does not resolve here.
    """

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.expiries: dict[str, int] = {}

    async def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value.encode()

    async def expire(self, key: str, seconds: int) -> None:
        self.expiries[key] = seconds


@pytest.fixture
def redis() -> _FakeRedis:
    return _FakeRedis()


@pytest.fixture
def client(life_list_app: Any, redis: _FakeRedis) -> Generator[TestClient]:
    """**The fake goes in after the lifespan, not before.** `create_redis_cache_pool` assigns
    `cache.client` on startup, so a patch applied around `TestClient(...)` is overwritten by
    the app itself and every request below then tries to reach the compose hostname."""
    life_list_app.dependency_overrides[get_current_user] = lambda: CURRENT_USER
    life_list_app.dependency_overrides[async_get_db] = lambda: MagicMock()
    with TestClient(life_list_app) as test_client, patch("src.app.core.utils.cache.client", redis):
        yield test_client
    life_list_app.dependency_overrides = {}


def _row(name: str) -> dict[str, Any]:
    return {
        "uuid": str(uuid7()),
        "scientific_name": name,
        "common_name": None,
        "rank": "Species",
        "photo_sha256": None,
        "dive_count": 1,
        "first_seen": datetime(2026, 1, 2, 9, 0, tzinfo=UTC).isoformat(),
        "last_seen": datetime(2026, 1, 2, 9, 0, tzinfo=UTC).isoformat(),
    }


def _service_answering(by_call: dict[tuple[int, str | None], str]) -> Any:
    """A stub that answers differently per (offset, search), so a request served from the
    wrong cache entry comes back with visibly the wrong rows rather than merely the wrong
    count."""

    async def service(*, db: Any, user_id: int, offset: int, limit: int, search: str | None = None) -> dict[str, Any]:
        return {"data": [_row(by_call[(offset, search)])], "total_count": 1}

    return patch.object(users_module, "species_life_list", service)


def _stub_service(rows: list[dict[str, Any]] | None = None, total: int = 0) -> Any:
    return patch.object(
        users_module, "species_life_list", AsyncMock(return_value={"data": rows or [], "total_count": total})
    )


class TestTheCacheKeyCarriesTheWholeQuery:
    """Asserted on the key template rather than on two live requests, because the live version
    of this check passes whether or not the key is right whenever it runs outside the 60 s TTL -
    and a test that is only sometimes meaningful is worse than none. The end-to-end walk is in
    the plan's live verification, where a human runs both requests inside a minute.
    """

    def test_page_two_is_not_served_page_ones_rows(self, client: TestClient, redis: _FakeRedis) -> None:
        """The defect itself, driven through the real decorator against a cache that really
        stores: a key without `page` serves page 1 for page 2, and the only visible symptom is
        a list that stops advancing."""
        with _service_answering({(0, None): "Page one species", (10, None): "Page two species"}):
            first = client.get(LIFE_LIST_PATH, params={"page": 1}).json()
            second = client.get(LIFE_LIST_PATH, params={"page": 2}).json()

        assert first["data"][0]["scientific_name"] == "Page one species"
        assert second["data"][0]["scientific_name"] == "Page two species"

    def test_a_bigger_page_is_not_served_the_smaller_ones_entry(self, client: TestClient, redis: _FakeRedis) -> None:
        """`items_per_page` changes the result set as surely as `page` does - the offsets differ
        the moment either moves."""
        with _service_answering({(0, None): "Ten at a time"}):
            client.get(LIFE_LIST_PATH, params={"page": 1, "items_per_page": 10})
        with _service_answering({(0, None): "A hundred at a time"}):
            larger = client.get(LIFE_LIST_PATH, params={"page": 1, "items_per_page": 100}).json()

        assert larger["data"][0]["scientific_name"] == "A hundred at a time"

    def test_a_search_does_not_serve_the_unfiltered_list_and_vice_versa(
        self, client: TestClient, redis: _FakeRedis
    ) -> None:
        """Both directions, because the failure is symmetric and either order hides the other:
        the search served the whole list, and the whole list then served the search's rows.
        Run back to back here rather than as a live check, where anything outside the 60 s TTL
        passes whether or not the key is right."""
        with _service_answering({(0, None): "Everything", (0, "moray"): "Just the moray"}):
            unfiltered = client.get(LIFE_LIST_PATH).json()
            searched = client.get(LIFE_LIST_PATH, params={"search": "moray"}).json()
            again = client.get(LIFE_LIST_PATH).json()

        assert unfiltered["data"][0]["scientific_name"] == "Everything"
        assert searched["data"][0]["scientific_name"] == "Just the moray"
        assert again["data"][0]["scientific_name"] == "Everything"

    def test_each_distinct_query_gets_its_own_entry(self, client: TestClient, redis: _FakeRedis) -> None:
        """The mechanism behind the three above, asserted once: four different queries, four
        keys. A prefix missing a placeholder collapses them onto one."""
        with _service_answering({(0, None): "a", (10, None): "b", (0, "moray"): "c", (0, "clownfish"): "d"}):
            client.get(LIFE_LIST_PATH, params={"page": 1})
            client.get(LIFE_LIST_PATH, params={"page": 2})
            client.get(LIFE_LIST_PATH, params={"search": "moray"})
            client.get(LIFE_LIST_PATH, params={"search": "clownfish"})

        assert len(redis.store) == 4

    def test_it_hangs_under_the_prefix_the_dive_invalidator_already_sweeps(self) -> None:
        """`invalidate_dive_caches()` deletes exactly `user_{id}_dives:*` and `user_{id}_dive:*`
        and deliberately not a wider pattern, so a namespace of its own would be a third
        pattern to remember there. The bug from forgetting is specific here: `species_seen` is
        uncached and drops instantly on a dive delete, so the dashboard tile and this list
        would disagree - breaking the very equality that makes them checkable against each
        other."""
        assert SPECIES_LIFE_LIST_CACHE_KEY_PREFIX.startswith("user_{user_id}_dives:")

    def test_every_placeholder_is_a_parameter_the_route_actually_passes(self) -> None:
        """`@cache` looks each placeholder up as a **keyword** argument, so one that no caller
        passes raises at key construction rather than degrading. This is what would fail if the
        cached helper's signature and its key template ever drifted apart."""
        import inspect

        # `@cache` uses `functools.wraps`, so the signature reported here is the undecorated
        # helper's - which is exactly the one whose keyword arguments the key is filled from.
        parameters = set(inspect.signature(users_module._cached_species_life_list).parameters)

        for placeholder in ("user_id", "page", "items_per_page", "search"):
            assert placeholder in parameters


class TestTheRoute:
    def test_it_clamps_an_absurd_page_size(self, client: TestClient) -> None:
        with _stub_service() as service:
            client.get(LIFE_LIST_PATH, params={"items_per_page": 999_999})

        assert service.await_args.kwargs["limit"] == 100

    def test_it_floors_a_negative_page(self, client: TestClient) -> None:
        with _stub_service() as service:
            client.get(LIFE_LIST_PATH, params={"page": -3})

        assert service.await_args.kwargs["offset"] == 0

    def test_a_blank_search_term_is_no_search_at_all(self, client: TestClient) -> None:
        """Normalized at the route so the shape the cache key is built from is the shape the
        route accepted. An all-whitespace term reaching the service would become a `%%`
        pattern - the unfiltered list, cached under a different key."""
        with _stub_service() as service:
            client.get(LIFE_LIST_PATH, params={"search": "   "})

        assert service.await_args.kwargs["search"] is None

    def test_a_search_term_has_its_whitespace_collapsed(self, client: TestClient) -> None:
        with _stub_service() as service:
            client.get(LIFE_LIST_PATH, params={"search": "  giant   moray "})

        assert service.await_args.kwargs["search"] == "giant moray"

    def test_it_answers_the_paginated_envelope(self, client: TestClient) -> None:
        row = {
            "uuid": str(uuid7()),
            "scientific_name": "Amphiprion ocellaris",
            "common_name": "Ocellaris clownfish",
            "rank": "Species",
            "photo_sha256": "a" * 64,
            "dive_count": 3,
            "first_seen": datetime(2026, 1, 2, 9, 0, tzinfo=UTC).isoformat(),
            "last_seen": datetime(2026, 3, 4, 9, 0, tzinfo=UTC).isoformat(),
        }
        with _stub_service([row], total=1):
            response = client.get(LIFE_LIST_PATH)

        assert response.status_code == 200
        body = response.json()
        assert body["total_count"] == 1
        assert body["data"][0]["photo_sha256"] == "a" * 64
        assert body["data"][0]["dive_count"] == 3

    def test_it_takes_no_user_uuid(self, life_list_app: Any) -> None:
        """Always the caller's own account, like the rest of `/user/...`. There is no uuid to
        get an ownership check backwards on - which is why this route is absent from
        `test_ownership.py`'s sweep rather than allowlisted in it.

        Through `iter_api_routes` rather than `app.routes`: since FastAPI 0.141 the latter
        holds two lazy `_IncludedRouter` wrappers and the prefixed routes are composed on
        demand, so iterating it directly finds nothing at all."""
        route = next(r for r in iter_api_routes(life_list_app) if r.path == "/api/v1/user/species")

        assert "{uuid}" not in route.path
        assert "user_uuid" not in {field.name for field in route.dependant.query_params}


@pytest.mark.skipif(not db_available(), reason="requires a database")
class TestTheAggregate:
    """The SQL, executed. A mocked session tests the code around a query and never the query,
    and everything interesting here is in the query."""

    def _log(self, db: Session, diver: Any, species: Any, *days: int, offset_minutes: int = 0) -> None:
        for day in days:
            dive = create_dive(db, diver)
            dive.start_time = datetime(2026, 6, day, 9, 0, tzinfo=UTC)
            dive.utc_offset_minutes = offset_minutes
            db.add(DiveSpecies(dive_id=dive.id, species_id=species.id, position=0))
        db.commit()

    @pytest.mark.asyncio
    async def test_one_row_per_species_with_its_whole_history(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        clownfish = create_species(db)
        self._log(db, diver, clownfish, 1, 5, 9)

        page = await species_life_list(async_db, user_id=diver.id, offset=0, limit=10)

        assert page["total_count"] == 1
        row = page["data"][0]
        assert row["dive_count"] == 3
        assert row["first_seen"] == datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
        assert row["last_seen"] == datetime(2026, 6, 9, 9, 0, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_the_dates_carry_the_offset_the_dives_were_logged_in(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """**A dive displays in the timezone it was logged in**, and this endpoint is bound by
        that contract like every other dive-derived surface. A `timestamptz` stores only an
        absolute instant, so a dive logged at 09:00 in Bangkok is 02:00 UTC — reporting the raw
        instant would show the wrong local time here while `GET /dive/{uuid}` shows the right
        one for the same dive.

        The fixture logs at 09:00 UTC with a +07:00 offset, so a correct answer reads 16:00
        +07:00 — the same instant, the diver's own clock. Zero-offset fixtures cannot tell the
        two apart, which is why every other test in this class was blind to it."""
        diver = create_user(db)
        species = create_species(db)
        self._log(db, diver, species, 1, 9, offset_minutes=420)

        page = await species_life_list(async_db, user_id=diver.id, offset=0, limit=10)

        row = page["data"][0]
        assert row["first_seen"].utcoffset() == timedelta(minutes=420)
        assert row["last_seen"].utcoffset() == timedelta(minutes=420)
        assert row["first_seen"] == datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
        assert row["first_seen"].hour == 16

    @pytest.mark.asyncio
    async def test_each_date_carries_its_own_dives_offset(self, db: Session, async_db: AsyncSession) -> None:
        """The offset belongs to the *one* dive that produced the `min()` or the `max()`, which
        no aggregate over the offset column can identify on its own — a diver who logged their
        first sighting in the Red Sea and their most recent in Indonesia gets two different
        offsets from one row. A single `min(utc_offset_minutes)` would pass the test above and
        fail this one."""
        diver = create_user(db)
        species = create_species(db)
        self._log(db, diver, species, 1, offset_minutes=180)
        self._log(db, diver, species, 20, offset_minutes=480)

        page = await species_life_list(async_db, user_id=diver.id, offset=0, limit=10)

        row = page["data"][0]
        assert row["first_seen"].utcoffset() == timedelta(minutes=180)
        assert row["last_seen"].utcoffset() == timedelta(minutes=480)

    @pytest.mark.asyncio
    async def test_the_ordering_is_still_by_the_absolute_instant(self, db: Session, async_db: AsyncSession) -> None:
        """ "First seen" means the earliest dive chronologically, whatever local time it read as.
        Sorting on the offset-shifted value instead would reorder a diver's log by where they
        happened to be standing."""
        diver = create_user(db)
        species = create_species(db)
        self._log(db, diver, species, 1, offset_minutes=-600)
        self._log(db, diver, species, 2, offset_minutes=780)

        page = await species_life_list(async_db, user_id=diver.id, offset=0, limit=10)

        row = page["data"][0]
        assert row["first_seen"] == datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
        assert row["last_seen"] == datetime(2026, 6, 2, 9, 0, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_a_soft_deleted_dive_does_not_count(self, db: Session, async_db: AsyncSession) -> None:
        """**The failure direction that matters**: `dive_species` has no liveness of its own and
        its `dive_id` cascade never fires, because dives are soft-deleted and no `DELETE FROM
        dive` is ever issued. Forgetting to reach through shows a diver dives they deleted."""
        diver = create_user(db)
        species = create_species(db)
        self._log(db, diver, species, 1, 2)
        deleted = create_dive(db, diver, is_deleted=True)
        db.add(DiveSpecies(dive_id=deleted.id, species_id=species.id, position=0))
        db.commit()

        page = await species_life_list(async_db, user_id=diver.id, offset=0, limit=10)

        assert page["data"][0]["dive_count"] == 2

    @pytest.mark.asyncio
    async def test_a_species_seen_only_on_a_deleted_dive_leaves_the_list(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """The stronger form of the case above: not an undercount but a row that must not be
        there at all, which is also what makes `total_count` match `species_seen`."""
        diver = create_user(db)
        gone = create_species(db)
        deleted = create_dive(db, diver, is_deleted=True)
        db.add(DiveSpecies(dive_id=deleted.id, species_id=gone.id, position=0))
        db.commit()

        page = await species_life_list(async_db, user_id=diver.id, offset=0, limit=10)

        assert page["total_count"] == 0
        assert page["data"] == []

    @pytest.mark.asyncio
    async def test_another_divers_sightings_are_invisible(self, db: Session, async_db: AsyncSession) -> None:
        """The species catalog is global - two divers' dives embed the identical row - so the
        scoping here is entirely in the join to `dive`, with nothing on `species` to fall back
        on."""
        mine = create_user(db)
        theirs = create_user(db)
        shared = create_species(db)
        self._log(db, mine, shared, 1)
        self._log(db, theirs, shared, 2, 3)

        page = await species_life_list(async_db, user_id=mine.id, offset=0, limit=10)

        assert page["data"][0]["dive_count"] == 1

    @pytest.mark.asyncio
    async def test_the_order_is_most_recently_seen_first(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        older = create_species(db)
        newer = create_species(db)
        self._log(db, diver, older, 1)
        self._log(db, diver, newer, 20)

        page = await species_life_list(async_db, user_id=diver.id, offset=0, limit=10)

        assert [row["uuid"] for row in page["data"]] == [newer.uuid, older.uuid]

    @pytest.mark.asyncio
    async def test_the_photo_digest_rides_along(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        species = create_species(db, photo_sha256="f" * 64)
        self._log(db, diver, species, 1)

        page = await species_life_list(async_db, user_id=diver.id, offset=0, limit=10)

        assert page["data"][0]["photo_sha256"] == "f" * 64

    @pytest.mark.asyncio
    async def test_a_second_page_carries_different_rows(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        for day in (1, 2, 3):
            self._log(db, diver, create_species(db), day)

        first = await species_life_list(async_db, user_id=diver.id, offset=0, limit=2)
        second = await species_life_list(async_db, user_id=diver.id, offset=2, limit=2)

        assert len(first["data"]) == 2
        assert len(second["data"]) == 1
        assert not {row["uuid"] for row in first["data"]} & {row["uuid"] for row in second["data"]}
        assert first["total_count"] == second["total_count"] == 3

    @pytest.mark.asyncio
    async def test_search_matches_the_scientific_name(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        wanted = create_species(db, scientific_name="zzfixture Gymnothorax javanicus")
        self._log(db, diver, wanted, 1)
        self._log(db, diver, create_species(db), 2)

        page = await species_life_list(async_db, user_id=diver.id, offset=0, limit=10, search="javanicus")

        assert [row["uuid"] for row in page["data"]] == [wanted.uuid]
        assert page["total_count"] == 1

    @pytest.mark.asyncio
    async def test_search_matches_an_alias_without_multiplying_the_counts(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """The reason the alias match is an `EXISTS` rather than a join. A species with three
        matching aliases joined in would produce three rows per dive, and `dive_count` would
        come back as dives x aliases - silently, and only for the species a diver searched
        for."""
        from src.app.models.species_name import SpeciesName

        diver = create_user(db)
        species = create_species(db)
        for alias in ("zzfixture giant moray", "zzfixture moray eel", "zzfixture moray"):
            db.add(SpeciesName(species_id=species.id, name=alias, kind="common", source="worms", language_code="eng"))
        db.commit()
        self._log(db, diver, species, 1, 2)

        page = await species_life_list(async_db, user_id=diver.id, offset=0, limit=10, search="zzfixture moray")

        assert page["total_count"] == 1
        assert page["data"][0]["dive_count"] == 2

    @pytest.mark.asyncio
    async def test_a_wildcard_typed_by_the_diver_is_escaped(self, db: Session, async_db: AsyncSession) -> None:
        """The decoy has to be a row that matches **only** if the wildcard is live, or the test
        passes with `escape_like` removed - the mistake this repo has already made twice and
        written up."""
        diver = create_user(db)
        literal = create_species(db, scientific_name="zzfixture 100% coral")
        decoy = create_species(db, scientific_name="zzfixture 100 and then coral")
        self._log(db, diver, literal, 1)
        self._log(db, diver, decoy, 2)

        page = await species_life_list(async_db, user_id=diver.id, offset=0, limit=10, search="100% coral")

        assert [row["uuid"] for row in page["data"]] == [literal.uuid]

    @pytest.mark.asyncio
    async def test_the_total_equals_the_species_seen_stat(self, db: Session, async_db: AsyncSession) -> None:
        """**The cheapest end-to-end check this feature has.** The dashboard tile is
        `COUNT(DISTINCT species_id)` over the same join, computed on the *write* path by
        `recalculate_dive_stats`, so the two agreeing for any diver is what says both reach
        through to `Dive.is_deleted` the same way. They are computed by different code in
        different places and nothing but this ties them together."""
        diver = create_user(db)
        for day, species in enumerate((create_species(db), create_species(db), create_species(db)), start=1):
            self._log(db, diver, species, day)
        buried = create_species(db)
        deleted = create_dive(db, diver, is_deleted=True)
        db.add(DiveSpecies(dive_id=deleted.id, species_id=buried.id, position=0))
        db.commit()

        await recalculate_dive_stats(async_db, user_id=diver.id)
        await async_db.commit()
        page = await species_life_list(async_db, user_id=diver.id, offset=0, limit=100)

        from src.app.models.user_dive_stats import UserDiveStats

        stats = (await async_db.execute(_stats_for(diver.id))).scalar_one()
        assert isinstance(stats, UserDiveStats)
        assert page["total_count"] == stats.species_seen == 3


def _stats_for(user_id: int) -> Any:
    from sqlalchemy import select

    from src.app.models.user_dive_stats import UserDiveStats

    return select(UserDiveStats).where(UserDiveStats.user_id == user_id)


# -------------- the dive filter --------------


class TestTheDiveFilter:
    """`GET /dives?species_uuid=`, the third `custom_filter` on that route."""

    def test_an_unknown_uuid_resolves_to_the_sentinel_rather_than_raising(self) -> None:
        """**`resolve_species_ids` returns `None`, not an empty map**, when any uuid is unknown -
        so a bare `.get` would raise `AttributeError` and answer 500 on exactly the case a
        filter has to answer with an empty page. The `(map or {})` spelling its two siblings
        already use is what avoids that."""
        from src.app.api.v1.dives import read_dives

        source = _route_source()
        assert "(species_map or {}).get(species_uuid, -1)" in source
        assert read_dives is not None

    def test_the_cache_key_grew_a_species_segment(self) -> None:
        """Every filter has to appear in the key, or two different result sets collapse onto
        one entry for sixty seconds. The three that were there before are asserted alongside it
        so a rewrite that dropped one fails here."""
        source = _route_source()
        for segment in ("trip_{trip_id}", "course_{course_id}", "site_{dive_site_id}", "gear_{gear_item_id}"):
            assert segment in source
        assert "species_{species_id}" in source

    def test_the_filter_is_registered_on_the_crud_instance(self) -> None:
        from src.app.crud.crud_dives import crud_dives

        assert "showing_species" in (crud_dives.custom_filters or {})

    def test_the_species_filter_takes_no_owner(self) -> None:
        """Unlike its two siblings, which scope by `user_id` because attaching another diver's
        gear to your dive is the thing they exist to prevent. The catalog is global, so there is
        no owner to compare against - and the outer query's own `user_id` filter is what bounds
        the result."""
        import inspect

        from src.app.crud.crud_species import resolve_species_ids

        assert "user_id" not in inspect.signature(resolve_species_ids).parameters


def _route_source() -> str:
    from pathlib import Path

    from src.app.api.v1 import dives as dives_module

    return Path(dives_module.__file__).read_text()


@pytest.mark.skipif(not db_available(), reason="requires a database")
class TestTheDiveFilterAgainstPostgres:
    @pytest.mark.asyncio
    async def test_it_returns_only_the_dives_carrying_that_species(self, db: Session, async_db: AsyncSession) -> None:
        """The `IN (subquery)` executed, not built. Its correctness is entirely in the SQL."""
        from src.app.crud.crud_dives import crud_dives

        diver = create_user(db)
        species = create_species(db)
        seen = create_dive(db, diver)
        create_dive(db, diver)
        db.add(DiveSpecies(dive_id=seen.id, species_id=species.id, position=0))
        db.commit()

        page = await crud_dives.get_multi(
            db=async_db, user_id=diver.id, is_deleted=False, id__showing_species=species.id, limit=10
        )

        assert [row["id"] for row in page["data"]] == [seen.id]

    @pytest.mark.asyncio
    async def test_the_unknown_sentinel_matches_nothing(self, db: Session, async_db: AsyncSession) -> None:
        """-1 can never be a real `species.id`, so a well-formed uuid naming no species comes
        back as an empty page rather than a 404 - which reveals nothing about whether that
        species exists."""
        from src.app.crud.crud_dives import crud_dives

        diver = create_user(db)
        species = create_species(db)
        dive = create_dive(db, diver)
        db.add(DiveSpecies(dive_id=dive.id, species_id=species.id, position=0))
        db.commit()

        page = await crud_dives.get_multi(
            db=async_db, user_id=diver.id, is_deleted=False, id__showing_species=-1, limit=10
        )

        assert page["data"] == []
        assert page["total_count"] == 0

    @pytest.mark.asyncio
    async def test_it_combines_with_the_other_filters(self, db: Session, async_db: AsyncSession) -> None:
        """Combinable, like the four that were already there - the filters are AND'd, so a
        species filter narrowing an already-narrowed page must not widen it back."""
        from src.app.crud.crud_dives import crud_dives
        from tests.helpers.generators import create_trip

        diver = create_user(db)
        trip = create_trip(db, diver)
        species = create_species(db)
        on_trip = create_dive(db, diver, trip=trip)
        off_trip = create_dive(db, diver)
        for dive in (on_trip, off_trip):
            db.add(DiveSpecies(dive_id=dive.id, species_id=species.id, position=0))
        db.commit()

        page = await crud_dives.get_multi(
            db=async_db,
            user_id=diver.id,
            is_deleted=False,
            trip_id=trip.id,
            id__showing_species=species.id,
            limit=10,
        )

        assert [row["id"] for row in page["data"]] == [on_trip.id]
