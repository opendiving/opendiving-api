"""`GET /<plural>/lookup`: the rows a picker lists, the ones the caller's dives used most
recently at or before `until` first, carrying what a dive form fills from a pick.

The pure half - the bound, the cache keys, the invalidation pairing - runs anywhere; the
ordering, the rows and the search parity run against Postgres.
"""

import ast
import fnmatch
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import TypeAdapter
from sqlalchemy import delete, event, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1 import contacts as contacts_module
from src.app.api.v1 import courses as courses_module
from src.app.api.v1 import dive_sites as dive_sites_module
from src.app.api.v1 import gear_items as gear_items_module
from src.app.api.v1 import people as people_module
from src.app.api.v1 import trips as trips_module
from src.app.core.utils.cache import _format_prefix
from src.app.core.utils.owned_resource_cache import OwnedResourceCache
from src.app.crud.crud_people import get_people_page
from src.app.models.course_person import CoursePerson
from src.app.models.dive import Dive
from src.app.models.dive_dive_site import DiveDiveSite
from src.app.models.dive_gear_item import DiveGearItem
from src.app.models.dive_person import DivePerson
from src.app.models.trip_part import TripPart
from src.app.models.trip_person import TripPerson
from src.app.models.user import User
from src.app.schemas.dive import DiveLocalStartTime
from src.app.schemas.dive_site import DiveSiteListSort
from src.app.schemas.lookup import LookupUntil, LookupUuids, lookup_bound
from tests.conftest import db_available
from tests.helpers.generators import (
    create_contact,
    create_course,
    create_dive_site,
    create_gear_item,
    create_person,
    create_trip,
)

V1_DIR = Path(__file__).resolve().parents[1] / "src" / "app" / "api" / "v1"


class TestTheBound:
    """Every shape a host's form holds becomes one UTC instant, the date's being the end of
    its day so the edited dive is never dropped from its own ranking."""

    @pytest.mark.parametrize(
        ("sent", "bound"),
        [
            ("2026-06-02T10:00:00+02:00", datetime(2026, 6, 2, 8, 0, tzinfo=UTC)),
            ("2026-06-02T10:00:00", datetime(2026, 6, 2, 10, 0, tzinfo=UTC)),
            ("2026-06-02", datetime(2026, 6, 2, 23, 59, 59, 999999, tzinfo=UTC)),
        ],
    )
    def test_each_shape_becomes_one_utc_instant(self, sent: str, bound: datetime) -> None:
        parsed: datetime | date = TypeAdapter(DiveLocalStartTime).validate_python(sent)

        assert lookup_bound(parsed) == bound
        assert lookup_bound(parsed).tzinfo is UTC  # type: ignore[union-attr]

    def test_two_spellings_of_one_instant_are_one_bound(self) -> None:
        east = datetime(2026, 6, 2, 10, 0, tzinfo=timezone(timedelta(hours=2)))
        west = datetime(2026, 6, 2, 3, 0, tzinfo=timezone(timedelta(hours=-5)))

        assert str(lookup_bound(east)) == str(lookup_bound(west))

    def test_none_is_no_bound(self) -> None:
        assert lookup_bound(None) is None

    def test_a_date_is_not_read_as_a_datetime(self) -> None:
        assert lookup_bound(date(2026, 6, 2)) == datetime(2026, 6, 2, 23, 59, 59, 999999, tzinfo=UTC)


# The pattern each resource's invalidator sweeps, for user 7.
_SWEPT_BY = {
    trips_module: OwnedResourceCache.list_cache_pattern("trips", 7),
    dive_sites_module: OwnedResourceCache.list_cache_pattern("dive_sites", 7),
    contacts_module: "user_7_contact*",
    courses_module: "user_7_course*",
    gear_items_module: "user_7_gear_*",
}


class TestTheCacheKeys:
    @pytest.mark.parametrize("module", list(_SWEPT_BY))
    def test_the_key_sits_under_the_sweep_that_drops_the_list(self, module: Any) -> None:
        prefix = module._LOOKUP_CACHE_KEY_PREFIX
        key = _format_prefix(
            prefix, {"user_id": 7, "page": 1, "items_per_page": 25, "search": None, "until": lookup_bound(None)}
        )

        assert prefix.endswith(":lookup:until_{until}")
        assert fnmatch.fnmatchcase(key, _SWEPT_BY[module])

    @pytest.mark.parametrize("module", [trips_module, dive_sites_module, contacts_module, courses_module])
    def test_the_key_extends_the_list_s_own(self, module: Any) -> None:
        caches = [value for value in vars(module).values() if isinstance(value, OwnedResourceCache)]

        assert len(caches) == 1
        assert module._LOOKUP_CACHE_KEY_PREFIX == caches[0].list_cache_key_prefix + ":lookup:until_{until}"

    def test_the_people_lookup_is_not_cached(self) -> None:
        source = (V1_DIR / "people.py").read_text()

        assert "@cache" not in source


class TestEveryDiveWriteDropsTheContactAndCourseLookups:
    """A contact's and a course's lookup rank by the dives naming them, so each dive write
    that drops the trip reads drops theirs too."""

    def test_each_one_drops_both(self) -> None:
        tree = ast.parse((V1_DIR / "dives.py").read_text())
        routes: dict[str, set[str]] = {}
        for node in tree.body:
            if isinstance(node, ast.AsyncFunctionDef):
                routes[node.name] = {
                    call.func.id
                    for call in ast.walk(node)
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                }

        dropping_trips = {name for name, calls in routes.items() if "invalidate_trip_caches" in calls}

        assert dropping_trips == {"write_dive", "patch_dive", "erase_dive", "merge_two_dives"}
        for name in dropping_trips:
            assert {"invalidate_contact_caches", "invalidate_course_caches"} <= routes[name], name


# ------------------------------------------------------------------ against Postgres


def _bound(day: int) -> datetime:
    return datetime(2026, 6, day, 9, 0, tzinfo=UTC)


def _dive(db: Session, user: User, start: datetime, **columns: Any) -> Dive:
    dive = Dive(user_id=user.id, dive_number=1, start_time=start, duration=1800, notes="", **columns)
    db.add(dive)
    db.commit()
    return dive


def _join(db: Session, row: Any) -> None:
    db.add(row)
    db.commit()


Lookup = Callable[[AsyncSession, User, str | None, datetime | None], Awaitable[list[dict[str, Any]]]]
Listed = Callable[[AsyncSession, User, str], Awaitable[set[Any]]]


@dataclass(frozen=True)
class _Resource:
    name: str
    make: Callable[[Session, User], Any]
    use: Callable[[Session, Dive, Any], None]
    lookup: Lookup
    listed: Listed
    # Makes the item findable by a field other than its name, and returns the term.
    searchable: Callable[[Session, Any], str]
    keys: frozenset[str]


async def _page(
    helper: Any, async_db: AsyncSession, user: User, search: str | None, bound: datetime | None
) -> list[dict[str, Any]]:
    result = await helper.__wrapped__(
        None, user_id=user.id, db=async_db, page=1, items_per_page=50, search=search, until=bound
    )
    rows: list[dict[str, Any]] = result["data"]
    return rows


def _uuids(result: dict[str, Any]) -> set[Any]:
    return {row["uuid"] for row in result["data"]}


# -- trips


def _use_trip(db: Session, dive: Dive, trip: Any) -> None:
    dive.trip_id = trip.id
    db.commit()


async def _lookup_trips(async_db: AsyncSession, user: User, search: str | None, bound: datetime | None) -> list[Any]:
    return await _page(trips_module._cached_lookup_trips, async_db, user, search, bound)


async def _list_trips(async_db: AsyncSession, user: User, term: str) -> set[Any]:
    return _uuids(
        await trips_module._cached_read_trips.__wrapped__(  # type: ignore[attr-defined]
            None, user_id=user.id, user_uuid=user.uuid, db=async_db, page=1, items_per_page=50, search=term
        )
    )


def _trip_searchable(db: Session, trip: Any) -> str:
    term = f"moalboal{uuid7().hex[-6:]}"
    _join(db, TripPart(trip_id=trip.id, position=1, name=term.upper()))
    return term


# -- dive sites


def _use_site(db: Session, dive: Dive, site: Any) -> None:
    _join(db, DiveDiveSite(dive_id=dive.id, dive_site_id=site.id, position=0))


async def _lookup_sites(async_db: AsyncSession, user: User, search: str | None, bound: datetime | None) -> list[Any]:
    return await _page(dive_sites_module._cached_lookup_dive_sites, async_db, user, search, bound)


async def _list_sites(async_db: AsyncSession, user: User, term: str) -> set[Any]:
    return _uuids(
        await dive_sites_module._cached_read_dive_sites.__wrapped__(  # type: ignore[attr-defined]
            None,
            user_id=user.id,
            user_uuid=user.uuid,
            db=async_db,
            page=1,
            items_per_page=50,
            search=term,
            tag_id=None,
            sort=DiveSiteListSort.NAME,
        )
    )


def _site_searchable(db: Session, site: Any) -> str:
    term = f"sunabe{uuid7().hex[-6:]}"
    site.other_names = [term.upper()]
    db.commit()
    return term


# -- contacts


def _use_contact(db: Session, dive: Dive, contact: Any) -> None:
    dive.contact_id = contact.id
    db.commit()


async def _lookup_contacts(async_db: AsyncSession, user: User, search: str | None, bound: datetime | None) -> list[Any]:
    return await _page(contacts_module._cached_lookup_contacts, async_db, user, search, bound)


async def _list_contacts(async_db: AsyncSession, user: User, term: str) -> set[Any]:
    return _uuids(
        await contacts_module._contact_cache._read_list_uncached(
            None,  # type: ignore[arg-type]
            user_id=user.id,
            user_uuid=user.uuid,
            db=async_db,
            page=1,
            items_per_page=50,
            search=term,
        )
    )


def _contact_searchable(db: Session, contact: Any) -> str:
    term = f"dahab{uuid7().hex[-6:]}"
    contact.address_city, contact.address_country = term.upper(), "Egypt"
    db.commit()
    return term


# -- people


def _use_person(db: Session, dive: Dive, person: Any) -> None:
    _join(db, DivePerson(dive_id=dive.id, person_id=person.id, position=0))


async def _lookup_people(async_db: AsyncSession, user: User, search: str | None, bound: datetime | None) -> list[Any]:
    """Through the route itself, there being no cached helper to reach past: the route takes
    the raw `until` and normalises it, and `lookup_bound` leaves a UTC instant as it is."""
    result = await people_module.lookup_people(
        request=None,  # type: ignore[arg-type]
        current_user={"id": user.id, "uuid": user.uuid},
        db=async_db,
        until=bound,
        page=1,
        items_per_page=50,
        search=search,
    )
    rows: list[Any] = result["data"]
    return rows


async def _list_people(async_db: AsyncSession, user: User, term: str) -> set[Any]:
    page = await get_people_page(async_db, user_id=user.id, offset=0, limit=50, search=term)
    return {person.uuid for person in page["data"]}


def _person_searchable(db: Session, person: Any) -> str:
    from tests.helpers.generators import create_user

    friend = create_user(db)
    person.linked_user_id = friend.id
    db.commit()
    return str(friend.username)


# -- courses


def _use_course(db: Session, dive: Dive, course: Any) -> None:
    dive.course_id = course.id
    db.commit()


async def _lookup_courses(async_db: AsyncSession, user: User, search: str | None, bound: datetime | None) -> list[Any]:
    return await _page(courses_module._cached_lookup_courses, async_db, user, search, bound)


async def _list_courses(async_db: AsyncSession, user: User, term: str) -> set[Any]:
    return _uuids(
        await courses_module._cached_read_courses.__wrapped__(  # type: ignore[attr-defined]
            None,
            user_id=user.id,
            user_uuid=user.uuid,
            db=async_db,
            page=1,
            items_per_page=50,
            search=term,
            date_from=None,
            date_to=None,
            agency=None,
            status=None,
        )
    )


def _course_searchable(db: Session, course: Any) -> str:
    return str(course.name.split()[-1]).upper()


# -- gear


def _use_gear(db: Session, dive: Dive, item: Any) -> None:
    _join(db, DiveGearItem(dive_id=dive.id, gear_item_id=item.id, position=0))


async def _lookup_gear(async_db: AsyncSession, user: User, search: str | None, bound: datetime | None) -> list[Any]:
    return await _page(gear_items_module._cached_lookup_gear_items, async_db, user, search, bound)


async def _list_gear(async_db: AsyncSession, user: User, term: str) -> set[Any]:
    return _uuids(
        await gear_items_module._cached_read_gear_items.__wrapped__(  # type: ignore[attr-defined]
            None,
            user_id=user.id,
            user_uuid=user.uuid,
            db=async_db,
            page=1,
            items_per_page=50,
            include_archived=False,
            search=term,
        )
    )


def _gear_searchable(db: Session, item: Any) -> str:
    term = f"apeks{uuid7().hex[-6:]}"
    item.brand = term.upper()
    db.commit()
    return term


RESOURCES = [
    _Resource(
        "trips",
        create_trip,
        _use_trip,
        _lookup_trips,
        _list_trips,
        _trip_searchable,
        frozenset({"uuid", "name", "people"}),
    ),
    _Resource(
        "dive_sites",
        create_dive_site,
        _use_site,
        _lookup_sites,
        _list_sites,
        _site_searchable,
        frozenset({"uuid", "name", "location", "water_type", "altitude", "entry_types"}),
    ),
    _Resource(
        "contacts",
        create_contact,
        _use_contact,
        _lookup_contacts,
        _list_contacts,
        _contact_searchable,
        frozenset({"uuid", "name", "address"}),
    ),
    _Resource(
        "people",
        create_person,
        _use_person,
        _lookup_people,
        _list_people,
        _person_searchable,
        frozenset({"uuid", "name", "username"}),
    ),
    _Resource(
        "courses",
        create_course,
        _use_course,
        _lookup_courses,
        _list_courses,
        _course_searchable,
        frozenset({"uuid", "name", "contact_uuid", "people"}),
    ),
    _Resource(
        "gear_items",
        create_gear_item,
        _use_gear,
        _lookup_gear,
        _list_gear,
        _gear_searchable,
        frozenset({"uuid", "name", "brand", "type", "rented", "is_archived"}),
    ),
]


@pytest.mark.skipif(not db_available(), reason="No database connection available")
@pytest.mark.parametrize("resource", RESOURCES, ids=lambda resource: resource.name)
class TestTheLookup:
    @pytest.mark.asyncio
    async def test_last_use_at_or_before_until_first_then_newest_created(
        self, resource: _Resource, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        """Three used items and two unused, created in a known order. A dive after the bound
        counts for nothing, a deleted dive for nothing, another diver's for nothing."""
        names = ("old_unused", "a", "b", "c", "new_unused")
        items = {name: resource.make(db, diver) for name in names}
        created = datetime(2026, 1, 1, tzinfo=UTC)
        for n, name in enumerate(names):
            items[name].created_at = created + timedelta(minutes=n)
        db.commit()

        resource.use(db, _dive(db, diver, _bound(1)), items["a"])
        resource.use(db, _dive(db, diver, _bound(3)), items["b"])
        resource.use(db, _dive(db, diver, _bound(5)), items["c"])
        resource.use(db, _dive(db, diver, _bound(9), is_deleted=True), items["old_unused"])
        resource.use(db, _dive(db, other_diver, _bound(9)), items["new_unused"])

        def order(rows: list[dict[str, Any]]) -> list[str]:
            by_uuid = {item.uuid: name for name, item in items.items()}
            return [by_uuid[row["uuid"]] for row in rows]

        unbounded = await resource.lookup(async_db, diver, None, None)
        assert order(unbounded) == ["c", "b", "a", "new_unused", "old_unused"]

        bounded = await resource.lookup(async_db, diver, None, _bound(4))
        assert order(bounded) == ["b", "a", "new_unused", "c", "old_unused"]

        # Inclusive: the dive at the bound itself counts.
        assert order(await resource.lookup(async_db, diver, None, _bound(3)))[:2] == ["b", "a"]

    @pytest.mark.asyncio
    async def test_the_row_carries_these_keys_alone(
        self, resource: _Resource, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        resource.make(db, diver)

        rows = await resource.lookup(async_db, diver, None, None)

        assert [set(row) for row in rows] == [set(resource.keys)]

    @pytest.mark.asyncio
    async def test_the_search_finds_what_the_list_s_finds(
        self, resource: _Resource, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        wanted, _ = resource.make(db, diver), resource.make(db, diver)
        term = resource.searchable(db, wanted)
        # The route lowercases and strips; the helpers below it take the term as the route
        # hands it on.
        term = term.strip().lower()

        found = {row["uuid"] for row in await resource.lookup(async_db, diver, term, None)}

        assert found == {wanted.uuid}
        assert found == await resource.listed(async_db, diver, term)


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheSpecifics:
    @pytest.mark.asyncio
    async def test_a_date_only_dive_on_the_bound_s_day_counts(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """Stored at its day's midnight, labelled UTC; a bare date bound is that day's end."""
        dated, timed, later = create_trip(db, diver), create_trip(db, diver), create_trip(db, diver)
        date_only = _dive(db, diver, datetime(2026, 6, 2, tzinfo=UTC))
        db.execute(update(Dive).where(Dive.id == date_only.id).values(utc_offset_minutes=None, start_date_only=True))
        db.commit()
        _use_trip(db, date_only, dated)
        _use_trip(db, _dive(db, diver, datetime(2026, 6, 2, 22, 0, tzinfo=UTC)), timed)
        _use_trip(db, _dive(db, diver, datetime(2026, 6, 3, 1, 0, tzinfo=UTC)), later)

        rows = await _lookup_trips(async_db, diver, None, lookup_bound(date(2026, 6, 2)))

        assert [row["uuid"] for row in rows[:2]] == [timed.uuid, dated.uuid]

    @pytest.mark.asyncio
    async def test_archived_gear_is_never_listed(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        kept, archived = create_gear_item(db, diver), create_gear_item(db, diver, is_archived=True)
        _use_gear(db, _dive(db, diver, _bound(1)), archived)

        rows = await _lookup_gear(async_db, diver, None, None)

        assert [row["uuid"] for row in rows] == [kept.uuid]

    @pytest.mark.asyncio
    async def test_the_rows_carry_what_the_picker_shows(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        site = create_dive_site(db, diver)
        contact = create_contact(db, diver)
        contact.address_city, contact.address_country = "Dahab", "Egypt"
        db.commit()

        (site_row,) = await _lookup_sites(async_db, diver, None, None)
        (contact_row,) = await _lookup_contacts(async_db, diver, None, None)

        assert site_row["location"]["name"] == site.location_name
        assert (contact_row["address"]["city"], contact_row["address"]["country"]) == ("Dahab", "Egypt")


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestWhatTheFormFills:
    """What a dive form fills from a pick rides on the row, so it never reads the record."""

    @pytest.mark.asyncio
    async def test_a_site_carries_its_water_type_altitude_and_entry_types(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        recorded, bare = create_dive_site(db, diver), create_dive_site(db, diver)
        recorded.water_type, recorded.altitude, recorded.entry_types = "fresh", 372, ["shore", "boat"]
        db.commit()

        rows = {row["uuid"]: row for row in await _lookup_sites(async_db, diver, None, None)}

        filled = ("water_type", "altitude", "entry_types")
        assert [rows[recorded.uuid][key] for key in filled] == ["fresh", 372, ["shore", "boat"]]
        assert [rows[bare.uuid][key] for key in filled] == [None, None, []]

    @pytest.mark.asyncio
    async def test_a_course_carries_its_contact_and_its_people_in_their_order(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        contact = create_contact(db, diver)
        course, bare = create_course(db, diver), create_course(db, diver)
        course.contact_id = contact.id
        db.commit()
        buddy, instructor = create_person(db, diver), create_person(db, diver)
        # Inserted against their positions, so the order read back is the positions'.
        _join(db, CoursePerson(course_id=course.id, person_id=instructor.id, position=1, role="instructor"))
        _join(db, CoursePerson(course_id=course.id, person_id=buddy.id, position=0))

        rows = {row["uuid"]: row for row in await _lookup_courses(async_db, diver, None, None)}

        assert rows[course.uuid]["contact_uuid"] == contact.uuid
        assert rows[course.uuid]["people"] == [
            {"person_uuid": buddy.uuid, "role": None},
            {"person_uuid": instructor.uuid, "role": "instructor"},
        ]
        assert (rows[bare.uuid]["contact_uuid"], rows[bare.uuid]["people"]) == (None, [])

    @pytest.mark.asyncio
    async def test_a_trip_carries_its_people_in_their_order(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        trip, bare = create_trip(db, diver), create_trip(db, diver)
        guide, companion = create_person(db, diver), create_person(db, diver)
        _join(db, TripPerson(trip_id=trip.id, person_id=companion.id, position=1, role="companion"))
        _join(db, TripPerson(trip_id=trip.id, person_id=guide.id, position=0, role="guide"))

        rows = {row["uuid"]: row for row in await _lookup_trips(async_db, diver, None, None)}

        assert rows[trip.uuid]["people"] == [
            {"person_uuid": guide.uuid, "role": "guide"},
            {"person_uuid": companion.uuid, "role": "companion"},
        ]
        assert rows[bare.uuid]["people"] == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("make", "people_on", "people_table", "lookup"),
        [
            (
                create_course,
                lambda item, person: CoursePerson(course_id=item.id, person_id=person.id),
                "course_person",
                _lookup_courses,
            ),
            (
                create_trip,
                lambda item, person: TripPerson(trip_id=item.id, person_id=person.id),
                "trip_person",
                _lookup_trips,
            ),
        ],
        ids=["courses", "trips"],
    )
    async def test_a_page_s_people_are_one_query_however_many_rows(
        self,
        make: Callable[[Session, User], Any],
        people_on: Callable[[Any, Any], Any],
        people_table: str,
        lookup: Lookup,
        db: Session,
        async_db: AsyncSession,
        diver: User,
    ) -> None:
        def add_one() -> None:
            _join(db, people_on(make(db, diver), create_person(db, diver)))

        statements: list[str] = []

        def record(*args: Any) -> None:
            statements.append(args[2])

        add_one()
        # Once before counting, so the connection's own setup is not counted.
        await lookup(async_db, diver, None, None)
        engine = async_db.bind.sync_engine  # type: ignore[union-attr]
        event.listen(engine, "before_cursor_execute", record)
        try:
            await lookup(async_db, diver, None, None)
            for_one = len(statements)
            add_one()
            add_one()
            statements.clear()
            rows = await lookup(async_db, diver, None, None)
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert [len(row["people"]) for row in rows] == [1, 1, 1]
        assert len(statements) == for_one
        assert sum(f"FROM {people_table} " in statement for statement in statements) == 1


async def _filtered(route: Any, async_db: AsyncSession, user: User, uuids: list[Any], search: str | None = None) -> Any:
    """Through the route itself with no request: a filtered page that reached `@cache` would
    fail on it, so a pass is also the filter's page staying uncached."""
    return await route(
        request=None,
        current_user={"id": user.id, "uuid": user.uuid},
        db=async_db,
        until=None,
        page=1,
        items_per_page=50,
        search=search,
        uuid=uuids,
    )


_FILTERED: dict[str, tuple[Any, Callable[[Session, User], Any]]] = {
    "trips": (trips_module.lookup_trips, create_trip),
    "dive_sites": (dive_sites_module.lookup_dive_sites, create_dive_site),
    "courses": (courses_module.lookup_courses, create_course),
}


@pytest.mark.skipif(not db_available(), reason="No database connection available")
@pytest.mark.parametrize("name", list(_FILTERED))
class TestTheUuidFilter:
    """A form holds uuids no search produced; the filter resolves them in one request."""

    @pytest.mark.asyncio
    async def test_the_caller_s_rows_with_those_uuids_and_no_others(
        self, name: str, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        route, make = _FILTERED[name]
        wanted, also_wanted, _unnamed = make(db, diver), make(db, diver), make(db, diver)
        someone_else_s = make(db, other_diver)
        deleted = make(db, diver)
        deleted_uuid = deleted.uuid
        db.execute(delete(type(deleted)).where(type(deleted).id == deleted.id))
        db.commit()

        result = await _filtered(
            route, async_db, diver, [wanted.uuid, also_wanted.uuid, someone_else_s.uuid, deleted_uuid]
        )

        assert {row["uuid"] for row in result["data"]} == {wanted.uuid, also_wanted.uuid}
        assert result["total_count"] == 2

    @pytest.mark.asyncio
    async def test_it_narrows_a_search_as_well(
        self, name: str, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        route, make = _FILTERED[name]
        wanted, other = make(db, diver), make(db, diver)

        result = await _filtered(route, async_db, diver, [wanted.uuid, other.uuid], search=wanted.name.lower())

        assert [row["uuid"] for row in result["data"]] == [wanted.uuid]


class TestTheQueryParameter:
    """Through FastAPI's own parsing, on an app of one route declaring the lookups' `until`:
    the union of a datetime and a date has to reach `lookup_bound` as the form sent it."""

    @staticmethod
    def _client() -> TestClient:
        app = FastAPI()

        @app.get("/lookup")
        def lookup(until: LookupUntil = None) -> dict[str, str]:
            return {"bound": str(lookup_bound(until))}

        return TestClient(app)

    @pytest.mark.parametrize(
        ("until", "bound"),
        [
            ("2026-06-02T10:00:00+02:00", "2026-06-02 08:00:00+00:00"),
            ("2026-06-02T10:00:00", "2026-06-02 10:00:00+00:00"),
            ("2026-06-02", "2026-06-02 23:59:59.999999+00:00"),
            (None, "None"),
        ],
    )
    def test_every_shape_a_form_holds_is_accepted(self, until: str | None, bound: str) -> None:
        response = self._client().get("/lookup", params={} if until is None else {"until": until})

        assert response.status_code == 200, response.text
        assert response.json() == {"bound": bound}

    def test_anything_else_is_a_422(self) -> None:
        assert self._client().get("/lookup", params={"until": "yesterday"}).status_code == 422


class TestTheUuidParameter:
    """Repeated once per uuid, as FastAPI reads a list from a query string."""

    @staticmethod
    def _client() -> TestClient:
        app = FastAPI()

        @app.get("/lookup")
        def lookup(uuid: LookupUuids = None) -> dict[str, list[str] | None]:
            return {"uuids": None if uuid is None else [str(each) for each in uuid]}

        return TestClient(app)

    def test_each_repeat_is_one_uuid(self) -> None:
        sent = [str(uuid7()), str(uuid7())]

        response = self._client().get("/lookup", params={"uuid": sent})

        assert response.status_code == 200, response.text
        assert response.json() == {"uuids": sent}

    def test_absent_is_no_filter(self) -> None:
        assert self._client().get("/lookup").json() == {"uuids": None}

    def test_no_more_than_a_page_holds(self) -> None:
        client = self._client()

        assert client.get("/lookup", params={"uuid": [str(uuid7()) for _ in range(100)]}).status_code == 200
        assert client.get("/lookup", params={"uuid": [str(uuid7()) for _ in range(101)]}).status_code == 422

    def test_a_malformed_uuid_is_a_422(self) -> None:
        assert self._client().get("/lookup", params={"uuid": "not-a-uuid"}).status_code == 422
