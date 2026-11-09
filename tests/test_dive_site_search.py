"""`GET /dive-sites/search`, the two points documents, and the revision under them, against real
Postgres as two accounts.

The suite's database keeps every Commons record any test made, so each test names its rows with a
marker no other row carries, or puts them somewhere on the globe no other test does and narrows
the search to that view.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

import asyncio
import random
import time
import uuid as uuid_pkg
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from src.app.api import router
from src.app.api.dependencies import get_current_user
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.core.setup import create_application
from src.app.core.utils import cache as cache_module
from src.app.crud.crud_dive_site_search import Box, box_around, name_contains
from src.app.crud.crud_users import read_account
from src.app.models.dive import Dive
from src.app.models.dive_dive_site import DiveDiveSite
from src.app.models.dive_site import DiveSite
from src.app.models.dive_site_proposal import DiveSiteProposal
from src.app.models.dive_site_tag import DiveSiteTag
from src.app.models.trip_part import TripPart
from src.app.models.user import User
from src.app.schemas.geocoding import GeocodeResult
from src.app.services import dive_site_search
from tests.conftest import db_available
from tests.helpers.fake_redis import FakeRedis
from tests.helpers.generators import (
    create_commons_site,
    create_dive_site,
    create_tag,
    create_trip,
    save_dive_site,
)

pytestmark = [pytest.mark.skipif(not db_available(), reason="No database connection available")]

OSM_CREDIT = "[Data © OpenStreetMap contributors, ODbL 1.0.](https://osm.org/copyright)"


def _marked(label: str = "") -> str:
    """A word no other row holds, of letters alone so a typo of it is still one word, and with no
    fixed prefix, which would make two markers resemble each other enough for the typo tier."""
    return label + "".join(random.choices("bcdfghjklmnpqrstvwxz", k=11)).capitalize()


def _somewhere() -> tuple[float, float]:
    """A point no other test's rows are near."""
    return round(random.uniform(-60, 60), 4), round(random.uniform(-170, 170), 4)


def _view(latitude: float, longitude: float, span: float = 0.5) -> dict[str, float]:
    return {"south": latitude - span, "north": latitude + span, "west": longitude - span, "east": longitude + span}


@dataclass
class Sign:
    account: dict[str, Any]
    accounts: dict[int, dict[str, Any]]

    def as_(self, user: User) -> None:
        self.account = self.accounts[user.id]


@pytest.fixture(scope="module")
def search_app() -> Any:
    return create_application(router=router, settings=settings, apply_migrations_on_start=False)


@pytest_asyncio.fixture
async def signed(
    search_app: Any, async_sessions: async_sessionmaker[AsyncSession], diver: User, other_diver: User
) -> AsyncGenerator[Sign]:
    async with async_sessions() as db:
        accounts = {
            user.id: cast(dict[str, Any], await read_account(db, uuid=user.uuid)) for user in (diver, other_diver)
        }
    sign = Sign(account=accounts[diver.id], accounts=accounts)
    search_app.dependency_overrides[get_current_user] = lambda: sign.account
    yield sign
    search_app.dependency_overrides = {}


@pytest_asyncio.fixture
async def http(
    search_app: Any, async_sessions: async_sessionmaker[AsyncSession], signed: Sign, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[httpx.AsyncClient]:
    monkeypatch.setattr(cache_module, "client", FakeRedis())

    async def per_request() -> AsyncGenerator[AsyncSession]:
        async with async_sessions() as session:
            yield session

    search_app.dependency_overrides[async_get_db] = per_request
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=search_app), base_url="http://test/api/v1"
    ) as client:
        yield client


PlaceSearch = Callable[[str], Awaitable[list[GeocodeResult]]]


@pytest.fixture(autouse=True)
def no_places(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test here reaches a real geocoder: one that wants places installs them with `geocoder`."""

    async def nothing(query: str) -> list[GeocodeResult]:
        return []

    monkeypatch.setattr(dive_site_search, "search_places", nothing)


@pytest.fixture
def geocoder(monkeypatch: pytest.MonkeyPatch) -> Callable[[PlaceSearch], list[str]]:
    """Answer place searches with this function, switched on whatever the instance's settings say;
    returns the list of the texts it was asked."""

    def install(answer: PlaceSearch) -> list[str]:
        asked: list[str] = []

        async def search_places(query: str) -> list[GeocodeResult]:
            asked.append(query)
            return await answer(query)

        monkeypatch.setattr(dive_site_search, "search_places", search_places)
        monkeypatch.setattr(dive_site_search, "place_search_enabled", lambda: True)
        return asked

    return install


def _place(name: str, latitude: float, longitude: float, *, box: Box | None = None) -> GeocodeResult:
    edges = {} if box is None else {"bbox_south": box.south, "bbox_north": box.north, "bbox_west": box.west}
    if box is not None:
        edges["bbox_east"] = box.east
    return GeocodeResult(
        latitude=latitude,
        longitude=longitude,
        location=f"{name}, Somewhere",
        name=name,
        attribution=OSM_CREDIT,
        source="osm",
        source_id=f"node/{random.randint(1, 10**9)}",
        **edges,
    )


def _answering(*places: GeocodeResult) -> PlaceSearch:
    async def answer(query: str) -> list[GeocodeResult]:
        return list(places)

    return answer


async def _search(http: httpx.AsyncClient, **params: Any) -> dict[str, Any]:
    response = await http.get("/dive-sites/search", params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _kinds(rows: list[dict[str, Any]]) -> list[str]:
    return [row["uuid"] if row["kind"] == "site" else f"place:{row['name']}" for row in rows]


def _own(db: Session, user: User, **values: Any) -> DiveSite:
    site = create_dive_site(db, user)
    for column, value in values.items():
        setattr(site, column, value)
    db.commit()
    return site


def _dive_at(db: Session, user: User, site: DiveSite, start: datetime, *, max_depth: float | None = None) -> Dive:
    dive = Dive(
        user_id=user.id,
        dive_number=1,
        start_time=start,
        submerged_time=1800,
        max_depth=max_depth,
        notes="",
    )
    db.add(dive)
    db.flush()
    db.add(DiveDiveSite(dive_id=dive.id, dive_site_id=site.id, position=0))
    db.commit()
    return dive


# -------------- the revision --------------


class TestTheRevision:
    @pytest.mark.asyncio
    async def test_the_functions_answer_with_an_empty_search_path(self, async_db: AsyncSession) -> None:
        """A plain dump restores with `search_path` empty, and rebuilds the index from there."""
        await async_db.execute(text("SET LOCAL search_path = ''"))

        folded = await async_db.scalar(
            text("SELECT public.dive_site_search_text('Côte d''Or', '[\"Shark Point\"]'::json)")
        )

        assert folded == "\x1fcote d'or\x1fshark point\x1f"

    @pytest.mark.asyncio
    async def test_the_name_condition_reads_the_trigram_index(self, async_db: AsyncSession) -> None:
        await async_db.execute(text("SET LOCAL enable_seqscan = off"))
        statement = select(DiveSite.id).where(name_contains("canyon"))
        compiled = statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})

        plan = "\n".join((await async_db.execute(text(f"EXPLAIN {compiled}"))).scalars())

        assert "ix_dive_site_search_text_trgm" in plan


# -------------- the tiers --------------


class TestTheTiers:
    @pytest.mark.asyncio
    async def test_every_tier_precedes_the_next_across_pages(
        self, http: httpx.AsyncClient, db: Session, diver: User, geocoder: Any
    ) -> None:
        term = _marked()
        latitude, longitude = _somewhere()
        near, far = (latitude, longitude), (latitude + 0.3, longitude)
        own_inside = _own(db, diver, name=f"Old {term} Wall", latitude=near[0], longitude=near[1])
        own_exact = _own(db, diver, name="Something", other_names=[term], latitude=far[0], longitude=far[1])
        own_prefix = _own(db, diver, name=f"{term} Point")
        commons_prefix_far = create_commons_site(db, name=f"{term} Far", latitude=far[0], longitude=far[1])
        commons_prefix_near = create_commons_site(db, name=f"{term} Near", latitude=near[0], longitude=near[1])
        commons_exact = create_commons_site(db, name=term.upper(), latitude=far[0], longitude=far[1])
        commons_inside = create_commons_site(db, name=f"The {term}", latitude=near[0], longitude=near[1])
        typo = create_commons_site(db, name=f"{term[:5]}a{term[6:]}", latitude=near[0], longitude=near[1])
        geocoder(_answering(_place("Town", latitude, longitude, box=Box(*_box(latitude, longitude)))))

        rows: list[dict[str, Any]] = []
        for page in (1, 2, 3, 4):
            body = await _search(
                http, q=term.lower(), latitude=latitude, longitude=longitude, page=page, items_per_page=3
            )
            rows += body["rows"]
            if not body["has_more"]:
                break

        assert _kinds(rows) == [
            str(own_exact.uuid),
            str(own_prefix.uuid),
            str(own_inside.uuid),
            str(commons_exact.uuid),
            str(commons_prefix_near.uuid),
            str(commons_prefix_far.uuid),
            str(commons_inside.uuid),
            "place:Town",
            str(typo.uuid),
        ]
        assert [row.get("match") for row in rows] == [
            "exact",
            "prefix",
            "contains",
            "exact",
            "prefix",
            "prefix",
            "contains",
            None,
            "fuzzy",
        ]
        # A site with no pin has no distance, and sorts after the pinned rows of its match.
        assert rows[1]["distance_m"] is None
        assert rows[4]["distance_m"] < rows[5]["distance_m"]

    @pytest.mark.asyncio
    async def test_a_page_ending_inside_the_commons_tier_carries_no_places(
        self, http: httpx.AsyncClient, db: Session, geocoder: Any
    ) -> None:
        term = _marked()
        for n in range(3):
            create_commons_site(db, name=f"{term} {n}")
        asked = geocoder(_answering(_place("Town", 1.0, 1.0)))

        first = await _search(http, q=term, items_per_page=2)
        second = await _search(http, q=term, items_per_page=2, page=2)

        assert [row["kind"] for row in first["rows"]] == ["site", "site"]
        assert first["has_more"] is True
        assert [row["kind"] for row in second["rows"]] == ["site", "place"]
        assert second["has_more"] is False
        # Only the page carrying the places asks for them.
        assert asked == [term]

    @pytest.mark.asyncio
    async def test_a_query_no_site_matches_has_the_places_first(self, http: httpx.AsyncClient, geocoder: Any) -> None:
        geocoder(_answering(_place("Somewhere", 1.0, 1.0)))

        body = await _search(http, q=_marked())

        assert [row["kind"] for row in body["rows"]][:1] == ["place"]

    @pytest.mark.parametrize(
        ("typed", "name"), [("Canion", "The Canyon"), ("Thistlgorm", "SS Thistlegorm"), ("reafs", "Lost Reefs")]
    )
    @pytest.mark.asyncio
    async def test_a_one_letter_typo_finds_the_site(
        self, http: httpx.AsyncClient, db: Session, typed: str, name: str
    ) -> None:
        latitude, longitude = _somewhere()
        site = create_commons_site(db, name=name, latitude=latitude, longitude=longitude)

        rows = (await _search(http, q=typed, **_view(latitude, longitude)))["rows"]

        assert [(row["uuid"], row["match"]) for row in rows if row["kind"] == "site"] == [(str(site.uuid), "fuzzy")]

    @pytest.mark.asyncio
    async def test_a_row_the_matched_tiers_list_is_not_listed_again(self, http: httpx.AsyncClient, db: Session) -> None:
        latitude, longitude = _somewhere()
        create_commons_site(db, name="Canyon", latitude=latitude, longitude=longitude)
        create_commons_site(db, name="Canyons", latitude=latitude, longitude=longitude)

        rows = (await _search(http, q="canyon", **_view(latitude, longitude)))["rows"]

        assert [row["match"] for row in rows] == ["exact", "prefix"]

    @pytest.mark.asyncio
    async def test_case_and_accents_fold_on_names_and_other_names(self, http: httpx.AsyncClient, db: Session) -> None:
        latitude, longitude = _somewhere()
        cote = create_commons_site(db, name="Côte d'Or", latitude=latitude, longitude=longitude)
        shark = create_commons_site(
            db, name="Ras Mohammed", other_names=["Shark Point"], latitude=latitude, longitude=longitude
        )

        by_cote = (await _search(http, q="cote", **_view(latitude, longitude)))["rows"]
        by_shark = (await _search(http, q="SHARK", **_view(latitude, longitude)))["rows"]

        assert [row["uuid"] for row in by_cote] == [str(cote.uuid)]
        assert [(row["uuid"], row["match"]) for row in by_shark] == [(str(shark.uuid), "prefix")]

    @pytest.mark.asyncio
    async def test_a_single_letter_runs_no_typo_tier_and_asks_no_geocoder(
        self, http: httpx.AsyncClient, db: Session, geocoder: Any
    ) -> None:
        latitude, longitude = _somewhere()
        create_commons_site(db, name="Q", latitude=latitude, longitude=longitude)
        asked = geocoder(_answering(_place("Town", 1.0, 1.0)))

        rows = (await _search(http, q="q", **_view(latitude, longitude)))["rows"]

        assert [row["match"] for row in rows] == ["exact"]
        assert asked == []


def _box(latitude: float, longitude: float, span: float = 0.1) -> tuple[float, float, float, float]:
    return latitude - span, latitude + span, longitude - span, longitude + span


# -------------- the places --------------


class TestThePlaces:
    @pytest.mark.asyncio
    async def test_a_place_counts_what_the_caller_can_see_inside_its_box(
        self, http: httpx.AsyncClient, db: Session, diver: User, other_diver: User, geocoder: Any
    ) -> None:
        latitude, longitude = _somewhere()
        _own(db, diver, latitude=latitude, longitude=longitude)
        create_commons_site(db, latitude=latitude, longitude=longitude)
        _own(db, other_diver, latitude=latitude, longitude=longitude)
        geocoder(_answering(_place("Boxed", latitude, longitude, box=Box(*_box(latitude, longitude)))))

        rows = (await _search(http, q=_marked()))["rows"]

        assert [(row["name"], row["sites_nearby"]) for row in rows] == [("Boxed", 2)]

    @pytest.mark.asyncio
    async def test_a_place_with_no_box_counts_within_25_km(
        self, http: httpx.AsyncClient, db: Session, geocoder: Any
    ) -> None:
        latitude, longitude = _somewhere()
        create_commons_site(db, latitude=latitude + 0.2, longitude=longitude)  # about 22 km
        create_commons_site(db, latitude=latitude + 0.25, longitude=longitude)  # about 28 km
        geocoder(_answering(_place("Point", latitude, longitude)))

        rows = (await _search(http, q=_marked()))["rows"]

        assert [(row["name"], row["sites_nearby"]) for row in rows] == [("Point", 1)]

    @pytest.mark.asyncio
    async def test_places_are_nearest_first_and_those_with_no_site_last(
        self, http: httpx.AsyncClient, db: Session, geocoder: Any
    ) -> None:
        latitude, longitude = _somewhere()
        create_commons_site(db, latitude=latitude + 2, longitude=longitude)
        geocoder(
            _answering(
                _place("Empty", latitude, longitude),
                _place("Far", latitude + 2, longitude),
                _place("Farther", latitude + 4, longitude),
            )
        )
        create_commons_site(db, latitude=latitude + 4, longitude=longitude)

        rows = (await _search(http, q=_marked(), latitude=latitude, longitude=longitude))["rows"]

        assert [row["name"] for row in rows] == ["Far", "Farther", "Empty"]
        assert rows[0]["distance_m"] == pytest.approx(222_390, rel=0.01)

    @pytest.mark.asyncio
    async def test_no_connection_is_held_while_the_geocoder_is_asked(
        self, http: httpx.AsyncClient, async_sessions: async_sessionmaker[AsyncSession], geocoder: Any
    ) -> None:
        engine = async_sessions.kw["bind"]
        checked_out: list[int] = []

        async def answer(query: str) -> list[GeocodeResult]:
            checked_out.append(engine.pool.checkedout())
            return []

        geocoder(answer)

        await _search(http, q=_marked())

        assert checked_out == [0]

    @pytest.mark.asyncio
    async def test_a_geocoder_past_its_budget_costs_the_places_alone(
        self, http: httpx.AsyncClient, db: Session, geocoder: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        term = _marked()
        site = create_commons_site(db, name=term)
        monkeypatch.setattr(settings, "DIVE_SITE_SEARCH_GEOCODER_BUDGET_SECONDS", 0.2)

        async def slow(query: str) -> list[GeocodeResult]:
            await asyncio.sleep(5)
            return [_place("Late", 1.0, 1.0)]

        geocoder(slow)
        started = time.monotonic()

        rows = (await _search(http, q=term))["rows"]

        assert time.monotonic() - started < 2
        assert [row["uuid"] for row in rows] == [str(site.uuid)]

    @pytest.mark.asyncio
    async def test_a_switched_off_geocoder_is_never_asked(
        self, http: httpx.AsyncClient, geocoder: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked = geocoder(_answering(_place("Town", 1.0, 1.0)))
        monkeypatch.setattr(dive_site_search, "place_search_enabled", lambda: False)

        rows = (await _search(http, q=_marked()))["rows"]

        assert rows == []
        assert asked == []

    @pytest.mark.asyncio
    async def test_a_spent_geocoder_budget_costs_the_places_and_not_the_answer(
        self, http: httpx.AsyncClient, db: Session, geocoder: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        term = _marked()
        site = create_commons_site(db, name=term)
        asked = geocoder(_answering(_place("Town", 1.0, 1.0)))
        monkeypatch.setattr(settings, "GEOCODER_RATE_LIMIT_PER_USER", 0)

        rows = (await _search(http, q=term))["rows"]

        assert [row["uuid"] for row in rows] == [str(site.uuid)]
        assert asked == []

    @pytest.mark.asyncio
    async def test_only_the_route_s_own_limit_refuses_a_request(
        self, http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "DIVE_SITE_SEARCH_RATE_LIMIT_PER_USER", 1)

        assert (await http.get("/dive-sites/search")).status_code == 200
        assert (await http.get("/dive-sites/search")).status_code == 429


# -------------- nothing typed --------------


class TestTheUntypedList:
    @pytest.mark.asyncio
    async def test_with_a_camera_it_is_the_nearest_pinned_sites_the_caller_can_see(
        self, http: httpx.AsyncClient, db: Session, diver: User, other_diver: User
    ) -> None:
        latitude, longitude = _somewhere()
        own = _own(db, diver, latitude=latitude + 0.03, longitude=longitude)
        saved = create_commons_site(db, latitude=latitude + 0.02, longitude=longitude)
        save_dive_site(db, saved, user=diver)
        commons = create_commons_site(db, latitude=latitude + 0.01, longitude=longitude)
        _own(db, other_diver, latitude=latitude, longitude=longitude)
        _own(db, diver)  # no pin
        outside = create_commons_site(db, latitude=latitude + 0.4, longitude=longitude)

        view = {**_view(latitude, longitude, span=0.1), "latitude": latitude, "longitude": longitude}
        first = await _search(http, **view, items_per_page=2)
        second = await _search(http, **view, items_per_page=2, page=2)

        assert [row["uuid"] for row in first["rows"] + second["rows"]] == [
            str(commons.uuid),
            str(saved.uuid),
            str(own.uuid),
        ]
        assert first["has_more"] is True and second["has_more"] is False
        assert str(outside.uuid) not in {row["uuid"] for row in first["rows"] + second["rows"]}
        assert all(row["match"] is None for row in first["rows"])

    @pytest.mark.asyncio
    async def test_a_box_crossing_the_antimeridian_holds_both_sides(self, http: httpx.AsyncClient, db: Session) -> None:
        latitude = round(random.uniform(-60, 60), 4)
        east_side = create_commons_site(db, latitude=latitude, longitude=179.95)
        west_side = create_commons_site(db, latitude=latitude, longitude=-179.95)
        create_commons_site(db, latitude=latitude, longitude=0.0)

        rows = (
            await _search(
                http,
                latitude=latitude,
                longitude=179.99,
                south=latitude - 0.01,
                north=latitude + 0.01,
                west=179.9,
                east=-179.9,
            )
        )["rows"]

        assert {row["uuid"] for row in rows} == {str(east_side.uuid), str(west_side.uuid)}

    @pytest.mark.asyncio
    async def test_without_a_camera_it_is_the_last_used_list_a_pinless_site_included(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        unused = _own(db, diver, latitude=1.0, longitude=1.0)
        pinless = _own(db, diver)
        _dive_at(db, diver, pinless, datetime.now(UTC) - timedelta(days=1))
        create_commons_site(db)

        body = await _search(http, **_view(1.0, 1.0))

        assert [row["uuid"] for row in body["rows"]] == [str(pinless.uuid), str(unused.uuid)]
        assert body["rows"][0]["distance_m"] is None
        assert body["total_count"] is None

    @pytest.mark.asyncio
    async def test_until_bounds_the_last_use(self, http: httpx.AsyncClient, db: Session, diver: User) -> None:
        earlier = _own(db, diver)
        later = _own(db, diver)
        _dive_at(db, diver, earlier, datetime(2026, 1, 1, tzinfo=UTC))
        _dive_at(db, diver, later, datetime(2026, 3, 1, tzinfo=UTC))

        rows = (await _search(http, until="2026-02-01"))["rows"]

        assert [row["uuid"] for row in rows] == [str(earlier.uuid), str(later.uuid)]


# -------------- your sites --------------


class TestYourSites:
    @pytest.mark.asyncio
    async def test_without_text_it_is_every_own_and_saved_site_last_dived_first(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        pinless = _own(db, diver)
        dived = create_commons_site(db)
        save_dive_site(db, dived, user=diver)
        _dive_at(db, diver, dived, datetime(2026, 5, 1, tzinfo=UTC), max_depth=24.0)
        create_commons_site(db)

        body = await _search(http, my="true", latitude=1.0, longitude=1.0)

        assert [row["uuid"] for row in body["rows"]] == [str(dived.uuid), str(pinless.uuid)]
        assert body["total_count"] == 2
        assert body["rows"][0]["dives"]["dive_count"] == 1
        assert body["rows"][0]["dives"]["max_dive_depth"] == 24.0
        assert body["rows"][1]["dives"]["dive_count"] == 0

    @pytest.mark.asyncio
    async def test_without_text_a_view_keeps_the_pinned_ones_inside_it_last_dived_first(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        latitude, longitude = _somewhere()
        dived_later = _own(db, diver, name="C site", latitude=latitude, longitude=longitude)
        saved = create_commons_site(db, name="B site", latitude=latitude + 0.1, longitude=longitude - 0.1)
        save_dive_site(db, saved, user=diver)
        never_dived = _own(db, diver, name="A site", latitude=latitude - 0.1, longitude=longitude + 0.1)
        _dive_at(db, diver, dived_later, datetime(2026, 5, 1, tzinfo=UTC))
        _dive_at(db, diver, saved, datetime(2026, 3, 1, tzinfo=UTC))
        outside = _own(db, diver, latitude=latitude + 2.0, longitude=longitude)
        pinless = _own(db, diver)
        create_commons_site(db, latitude=latitude, longitude=longitude)
        view = _view(latitude, longitude)

        first = await _search(http, my="true", items_per_page=2, **view)
        second = await _search(http, my="true", items_per_page=2, page=2, **view)

        assert [row["uuid"] for row in first["rows"] + second["rows"]] == [
            str(dived_later.uuid),
            str(saved.uuid),
            str(never_dived.uuid),
        ]
        assert first["total_count"] == 3 and first["has_more"] is True and second["has_more"] is False
        assert {str(outside.uuid), str(pinless.uuid)}.isdisjoint(row["uuid"] for row in first["rows"] + second["rows"])

    @pytest.mark.asyncio
    async def test_without_text_a_view_narrows_tag_commons_match_and_sort_too(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        latitude, longitude = _somewhere()
        record = create_commons_site(db, latitude=latitude + 5.0, longitude=longitude)
        matching = _own(
            db, diver, name="B site", external_ids=record.external_ids, latitude=latitude, longitude=longitude
        )
        _own(db, diver, external_ids=record.external_ids, latitude=latitude + 2.0, longitude=longitude)
        tag = create_tag(db, diver)
        tagged = _own(db, diver, name="A site", latitude=latitude, longitude=longitude)
        tagged_outside = _own(db, diver, latitude=latitude + 2.0, longitude=longitude)
        db.add_all(
            [
                DiveSiteTag(dive_site_id=tagged.id, tag_id=tag.id, position=0),
                DiveSiteTag(dive_site_id=tagged_outside.id, tag_id=tag.id, position=0),
            ]
        )
        db.commit()
        view = _view(latitude, longitude)

        by_tag = await _search(http, my="true", tag_uuid=str(tag.uuid), **view)
        by_match = await _search(http, my="true", commons_match="true", **view)
        by_name = await _search(http, my="true", sort="name", **view)

        assert [row["uuid"] for row in by_tag["rows"]] == [str(tagged.uuid)] and by_tag["total_count"] == 1
        assert [row["uuid"] for row in by_match["rows"]] == [str(matching.uuid)] and by_match["total_count"] == 1
        assert [row["uuid"] for row in by_name["rows"]] == [str(tagged.uuid), str(matching.uuid)]

    @pytest.mark.asyncio
    async def test_without_text_a_view_crossing_the_antimeridian_holds_both_sides(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        latitude = round(random.uniform(-60, 60), 4)
        east_side = _own(db, diver, name="A east", latitude=latitude, longitude=179.95)
        west_side = _own(db, diver, name="B west", latitude=latitude, longitude=-179.95)
        _own(db, diver, latitude=latitude, longitude=0.0)

        body = await _search(
            http,
            my="true",
            sort="name",
            south=latitude - 0.01,
            north=latitude + 0.01,
            west=179.9,
            east=-179.9,
        )

        assert [row["uuid"] for row in body["rows"]] == [str(east_side.uuid), str(west_side.uuid)]
        assert body["total_count"] == 2

    @pytest.mark.asyncio
    async def test_a_typed_word_matches_the_locality_too_after_the_names(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        latitude, longitude = _somewhere()
        town = _marked("Dahab")
        by_locality = _own(
            db, diver, name="Lighthouse", location_name=f"{town}, Egypt", latitude=latitude, longitude=longitude
        )
        by_name = _own(db, diver, name=f"{town} Reef", latitude=latitude, longitude=longitude)
        view = _view(latitude, longitude)

        mine = (await _search(http, q=town.lower(), my="true", **view))["rows"]
        everyone = (await _search(http, q=town.lower(), **view))["rows"]

        assert [(row["uuid"], row["match"]) for row in mine] == [
            (str(by_name.uuid), "prefix"),
            (str(by_locality.uuid), None),
        ]
        assert [row["uuid"] for row in everyone] == [str(by_name.uuid)]

    @pytest.mark.asyncio
    async def test_it_lists_no_record_the_caller_has_not_saved(self, http: httpx.AsyncClient, db: Session) -> None:
        term = _marked()
        create_commons_site(db, name=term)

        assert (await _search(http, q=term, my="true"))["rows"] == []

    @pytest.mark.asyncio
    async def test_tag_commons_match_and_sort_narrow_and_order_as_the_list(
        self, http: httpx.AsyncClient, db: Session, diver: User, other_diver: User
    ) -> None:
        record = create_commons_site(db)
        matching = _own(db, diver, name="B site", external_ids=record.external_ids)
        tagged = _own(db, diver, name="A site")
        tag = create_tag(db, diver)
        db.add(DiveSiteTag(dive_site_id=tagged.id, tag_id=tag.id, position=0))
        db.commit()
        foreign = create_tag(db, other_diver)

        by_tag = await _search(http, my="true", tag_uuid=str(tag.uuid))
        by_foreign_tag = await _search(http, my="true", tag_uuid=str(foreign.uuid))
        by_match = await _search(http, my="true", commons_match="true")
        by_name = await _search(http, my="true", sort="name")

        assert [row["uuid"] for row in by_tag["rows"]] == [str(tagged.uuid)]
        assert by_foreign_tag["rows"] == [] and by_foreign_tag["total_count"] == 0
        assert [row["uuid"] for row in by_match["rows"]] == [str(matching.uuid)]
        assert [row["uuid"] for row in by_name["rows"]] == [str(tagged.uuid), str(matching.uuid)]

    @pytest.mark.asyncio
    async def test_with_sort_the_places_follow_every_site_row(
        self, http: httpx.AsyncClient, db: Session, diver: User, geocoder: Any
    ) -> None:
        term = _marked()
        _own(db, diver, name=f"B {term}")
        _own(db, diver, name=f"A {term[:5]}a{term[6:]}")
        geocoder(_answering(_place("Town", 1.0, 1.0)))

        rows = (await _search(http, q=term, my="true", sort="name"))["rows"]

        assert [(row["kind"], row.get("match")) for row in rows] == [
            ("site", "fuzzy"),
            ("site", "contains"),
            ("place", None),
        ]

    @pytest.mark.parametrize(
        "narrowing", [{"tag_uuid": str(uuid_pkg.uuid4())}, {"commons_match": "false"}, {"sort": "name"}]
    )
    @pytest.mark.asyncio
    async def test_each_narrowing_without_my_is_a_422(self, http: httpx.AsyncClient, narrowing: dict[str, str]) -> None:
        assert (await http.get("/dive-sites/search", params=narrowing)).status_code == 422


# -------------- what a row carries --------------


class TestTheRow:
    @pytest.mark.asyncio
    async def test_a_proposed_site_reads_waiting(self, http: httpx.AsyncClient, db: Session, diver: User) -> None:
        term = _marked()
        site = _own(db, diver, name=term)
        db.add(
            DiveSiteProposal(
                user_id=diver.id,
                dive_site_id=site.id,
                kind="new_site",
                members={},
                attestation="I made it",
                attested_at=datetime.now(UTC),
            )
        )
        db.commit()

        rows = (await _search(http, q=term))["rows"]

        assert rows[0]["proposal"]["status"] == "waiting"

    @pytest.mark.asyncio
    async def test_another_account_s_layer_reaches_no_row(
        self, http: httpx.AsyncClient, db: Session, diver: User, other_diver: User
    ) -> None:
        term = _marked()
        record = create_commons_site(db, name=term)
        save_dive_site(db, record, user=other_diver)
        _dive_at(db, other_diver, record, datetime(2026, 5, 1, tzinfo=UTC))

        rows = (await _search(http, q=term))["rows"]

        assert [(row["saved"], row["dives"], row["owner"]) for row in rows] == [(False, None, "opendivingcommons")]
        assert "notes" not in rows[0] and "tags" not in rows[0]


class TestTheRequest:
    @pytest.mark.parametrize(
        "params",
        [
            {"latitude": 1.0},
            {"longitude": 1.0},
            {"south": 0.0, "north": 1.0, "west": 0.0},
            {"south": 2.0, "north": 1.0, "west": 0.0, "east": 1.0},
        ],
    )
    @pytest.mark.asyncio
    async def test_half_a_camera_or_a_partial_box_is_a_422(
        self, http: httpx.AsyncClient, params: dict[str, float]
    ) -> None:
        assert (await http.get("/dive-sites/search", params=params)).status_code == 422


# -------------- the points --------------


class TestThePoints:
    @pytest.mark.asyncio
    async def test_the_caller_s_points_are_their_pinned_sites_alone(
        self, http: httpx.AsyncClient, db: Session, diver: User, other_diver: User
    ) -> None:
        own = _own(db, diver, latitude=1.5, longitude=2.5)
        _own(db, diver)
        _own(db, other_diver, latitude=1.0, longitude=1.0)
        record = create_commons_site(db, latitude=3.0, longitude=4.0)

        before = (await http.get("/dive-sites/points")).json()
        assert (await http.put(f"/dive-site/{record.uuid}/saved")).status_code == 200
        after = (await http.get("/dive-sites/points")).json()

        assert before == {"points": [[2.5, 1.5, str(own.uuid), own.name, "account", None]]}
        assert sorted(after["points"]) == sorted(
            [
                [2.5, 1.5, str(own.uuid), own.name, "account", None],
                [4.0, 3.0, str(record.uuid), record.name, "opendivingcommons", None],
            ]
        )

    @pytest.mark.asyncio
    async def test_the_commons_document_is_the_same_bytes_for_two_accounts(
        self, http: httpx.AsyncClient, db: Session, signed: Sign, other_diver: User
    ) -> None:
        record = create_commons_site(db)
        create_commons_site(db, latitude=None, longitude=None)
        _own(db, other_diver, latitude=1.0, longitude=1.0)

        first = await http.get("/commons/dive-sites/points")
        signed.as_(other_diver)
        second = await http.get("/commons/dive-sites/points")

        assert first.status_code == 200 and first.content == second.content
        body = first.json()
        pin = [round(cast(float, record.longitude), 5), round(cast(float, record.latitude), 5)]
        assert [*pin, str(record.uuid), record.name, "openstreetmap"] in body["points"]
        assert all(len(point) == 5 for point in body["points"])
        assert body["sources"]["openstreetmap"] == OSM_CREDIT

    @pytest.mark.asyncio
    async def test_the_version_moves_on_an_insert_an_update_and_a_review(
        self, http: httpx.AsyncClient, db: Session
    ) -> None:
        async def etag() -> str:
            response = await http.get("/commons/dive-sites/points")
            assert response.status_code == 200
            return response.headers["etag"]

        start = await etag()
        repeat = await http.get("/commons/dive-sites/points", headers={"If-None-Match": start})
        record = create_commons_site(db)
        inserted = await etag()
        record.name = f"{record.name} renamed"
        record.updated_at = datetime.now(UTC) + timedelta(seconds=1)
        db.commit()
        updated = await etag()
        record.commons_reviewed_at = datetime.now(UTC) + timedelta(seconds=2)
        db.commit()
        reviewed = await etag()

        assert repeat.status_code == 304 and repeat.content == b""
        assert len({start, inserted, updated, reviewed}) == 4
        assert any(point[3] == record.name for point in (await http.get("/commons/dive-sites/points")).json()["points"])


# -------------- the trip lookup --------------


class TestTheTripLookupParts:
    @pytest.mark.asyncio
    async def test_a_row_carries_the_parts_the_trip_read_carries(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        trip = create_trip(db, diver)
        db.add(
            TripPart(
                trip_id=trip.id,
                position=1,
                start_date=date(2026, 6, 8),
                end_date=date(2026, 6, 9),
                name="Dahab, Egypt",
                latitude=28.5,
                longitude=34.5,
            )
        )
        db.commit()
        bare = create_trip(db, diver)
        db.execute(delete(TripPart).where(TripPart.trip_id == bare.id))
        db.commit()

        rows = (await http.get("/trips/lookup", params={"uuid": [str(trip.uuid), str(bare.uuid)]})).json()["data"]
        read = (await http.get(f"/trip/{trip.uuid}")).json()

        by_uuid = {row["uuid"]: row for row in rows}
        expected = [{key: part[key] for key in ("start_date", "end_date", "location")} for part in read["parts"]]
        assert by_uuid[str(trip.uuid)]["parts"] == expected
        assert by_uuid[str(trip.uuid)]["parts"][1]["location"]["name"] == "Dahab, Egypt"
        assert by_uuid[str(bare.uuid)]["parts"] == []


# -------------- without a database --------------


class TestTheBoxAroundAPoint:
    def test_it_wraps_across_the_antimeridian(self) -> None:
        box = box_around(0.0, 179.9, 25_000)

        assert box.west > box.east
        assert box.west == pytest.approx(179.675, abs=0.01)

    def test_near_a_pole_it_spans_every_longitude(self) -> None:
        box = box_around(89.9, 10.0, 25_000)

        assert (box.west, box.east, box.north) == (-180.0, 180.0, 90.0)
