"""A trip's candidates: the caller's live dives on no trip whose own local day one of the
trip's parts covers - listed with the trip's dives by `GET /trip/{uuid}/dives`, counted on
the trip read, and added by `POST /trip/{uuid}/dives`.

The request schema and the cache keys run anywhere. What the database decides runs against
Postgres: the local day is SQL arithmetic over each stored state of a dive's start, and every
row here is built as a model instance with its own instant and offset, because the log
builder in `tests/helpers/generators.py` fixes every row at 09:00 UTC.
"""

from datetime import UTC, date, datetime, timedelta, timezone
from fnmatch import fnmatch
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1 import trips as trips_module
from src.app.core.exceptions.http_exceptions import NotFoundException, UnprocessableEntityException
from src.app.core.utils import cache as cache_module
from src.app.core.utils.cache import across_builds, namespaced
from src.app.crud.crud_dives import DIVE_LOCAL_DAY, part_for_day
from src.app.models.dive import Dive
from src.app.models.trip import Trip
from src.app.models.trip_part import TripPart
from src.app.models.user import User
from src.app.schemas.trip import TripDiveAddRequest, TripPartInput, TripPartRead, TripUpdateRequest
from src.app.services.year_in_review import local_day
from tests.conftest import db_available
from tests.helpers.generators import create_contact

USER_ID = 7


class TestTheRequest:
    def test_names_nothing_for_every_candidate(self) -> None:
        scope = TripDiveAddRequest.model_validate({})

        assert (scope.dive_uuids, scope.part) == (None, None)

    def test_refuses_two_scopes(self) -> None:
        with pytest.raises(ValidationError, match="not both"):
            TripDiveAddRequest.model_validate({"dive_uuids": [str(uuid7())], "part": {"start_date": "2026-06-10"}})

    @pytest.mark.parametrize("name", ["dive_uuids", "part"])
    def test_refuses_a_null_scope_rather_than_reading_it_as_every_candidate(self, name: str) -> None:
        with pytest.raises(ValidationError, match="not null"):
            TripDiveAddRequest.model_validate({name: None})

    def test_refuses_a_part_named_by_no_date(self) -> None:
        with pytest.raises(ValidationError, match="named by its dates"):
            TripDiveAddRequest.model_validate({"part": {}})

    @pytest.mark.parametrize("count", [0, 101])
    def test_bounds_the_dives_named(self, count: int) -> None:
        with pytest.raises(ValidationError):
            TripDiveAddRequest.model_validate({"dive_uuids": [str(uuid7()) for _ in range(count)]})


def test_a_trip_whose_parts_carry_no_dates_attributes_no_day() -> None:
    assert str(part_for_day([TripPartRead(), TripPartRead()])) == "NULL"


class TestTheListKey:
    @pytest.mark.asyncio
    async def test_is_under_the_dives_prefix_every_dive_write_sweeps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        written: list[str] = []

        class _Recording:
            async def get(self, key: str) -> None:
                return None

            async def set(self, key: str, value: str) -> None:
                written.append(key)

            async def expire(self, key: str, seconds: int) -> None:
                return None

        async def _page(*_: Any, **__: Any) -> dict[str, Any]:
            return {"data": [], "total_count": 0}

        async def _parts(*_: Any, **__: Any) -> list[Any]:
            return []

        monkeypatch.setattr(trips_module, "get_parts_for_trip", _parts)
        monkeypatch.setattr(trips_module, "get_dives_page", _page)
        monkeypatch.setattr(cache_module, "client", _Recording())
        request = MagicMock()
        request.method = "GET"

        await trips_module._cached_read_trip_dives(
            request, user_id=USER_ID, user_uuid=uuid7(), db=MagicMock(), trip_id=11, page=2, items_per_page=25
        )

        (key,) = written
        assert key == namespaced(f"user_{USER_ID}_dives:trip_11:page_2:items_per_page:25:{USER_ID}")
        assert fnmatch(key, across_builds(f"user_{USER_ID}_dives:*"))


# ------------------------------------------------------------------ against Postgres

PLUS_7 = timezone(timedelta(hours=7))
MINUS_10 = timezone(timedelta(hours=-10))


def _dive(
    db: Session,
    user: User,
    start: datetime,
    *,
    offset: int | None = 0,
    date_only: bool = False,
    trip: Trip | None = None,
    **columns: Any,
) -> Dive:
    """A dive starting at `start`, whose offset - unless named - is the one `start` carries.

    A NULL offset is written by an `UPDATE`: the column's Python-side default of `0` would
    replace a `None` handed to the constructor."""
    if offset == 0 and start.utcoffset():
        offset = int(start.utcoffset().total_seconds() // 60)  # type: ignore[union-attr]
    dive = Dive(
        user_id=user.id,
        dive_number=1,
        start_time=start,
        utc_offset_minutes=offset or 0,
        trip_id=None if trip is None else trip.id,
        duration=1800,
        notes="",
        **columns,
    )
    db.add(dive)
    db.commit()
    if offset is None:
        db.execute(update(Dive).where(Dive.id == dive.id).values(utc_offset_minutes=None, start_date_only=date_only))
        db.commit()
        db.refresh(dive)
    return dive


def _on(db: Session, user: User, day: date, **columns: Any) -> Dive:
    """A dive at 09:00 UTC on `day`, whose local day is that day."""
    return _dive(db, user, datetime(day.year, day.month, day.day, 9, tzinfo=UTC), **columns)


def _trip(db: Session, user: User, *parts: tuple[date | None, date | None]) -> Trip:
    trip = Trip(user_id=user.id, name=f"Dahab {uuid7().hex[-8:]}", notes="")
    db.add(trip)
    db.commit()
    db.add_all(
        TripPart(trip_id=trip.id, position=position, start_date=start, end_date=end)
        for position, (start, end) in enumerate(parts)
    )
    db.commit()
    return trip


def _as(user: User) -> dict[str, Any]:
    return {"id": user.id, "uuid": user.uuid}


def _get() -> MagicMock:
    request = MagicMock()
    request.method = "GET"
    return request


class _ServingRedis:
    """Keeps what `@cache` writes, serves it back and sweeps by pattern, so a read after a
    write is stale unless something dropped it."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    async def set(self, key: str, value: str) -> None:
        self.store[key] = value.encode()

    async def expire(self, key: str, seconds: int) -> None:
        return None

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.store.pop(key, None)

    async def scan(self, cursor: int = 0, match: str = "*", count: int | None = None) -> tuple[int, list[str]]:
        return 0, [key for key in self.store if fnmatch(key, match)]


async def _listed(async_db: AsyncSession, user: User, trip: Trip, *, page: int = 1, size: int = 50) -> dict[str, Any]:
    result: dict[str, Any] = await trips_module.read_trip_dives(
        request=_get(), uuid=trip.uuid, current_user=_as(user), db=async_db, page=page, items_per_page=size
    )
    return result


async def _uuids(async_db: AsyncSession, user: User, trip: Trip) -> list[Any]:
    return [row["uuid"] for row in (await _listed(async_db, user, trip))["data"]]


async def _read(async_db: AsyncSession, user: User, trip: Trip) -> dict[str, Any]:
    read: dict[str, Any] = await trips_module.read_trip(
        request=_get(), uuid=trip.uuid, current_user=_as(user), db=async_db
    )
    return read


async def _counts(async_db: AsyncSession, user: User, trip: Trip) -> tuple[int, list[int]]:
    """The trip's candidate count and its parts', from its single read and the list page,
    which have to agree."""
    single = await _read(async_db, user, trip)
    page: dict[str, Any] = await trips_module.read_trips(
        request=_get(), current_user=_as(user), db=async_db, page=1, items_per_page=100, search=None
    )
    (listed,) = [row for row in page["data"] if row["uuid"] == single["uuid"]]
    counts = (single["candidate_count"], [part["candidate_count"] for part in single["parts"]])
    assert counts == (listed["candidate_count"], [part["candidate_count"] for part in listed["parts"]])
    return counts


async def _add(async_db: AsyncSession, user: User, trip: Trip, body: dict[str, Any]) -> int:
    result = await trips_module.add_trip_dives(
        request=MagicMock(),
        uuid=trip.uuid,
        scope=TripDiveAddRequest.model_validate(body),
        current_user=_as(user),
        db=async_db,
    )
    return result.added


JUNE = (date(2026, 6, 10), date(2026, 6, 12))


@pytest.fixture
def serving_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_module, "client", _ServingRedis())


@pytest.mark.skipif(not db_available(), reason="No database connection available")
@pytest.mark.usefixtures("serving_cache")
class TestTheLocalDay:
    """The trip's one part runs from the 10th to the 12th."""

    @pytest.fixture
    def rows(self, db: Session, diver: User) -> dict[str, Dive]:
        return {
            "early_plus_7": _dive(db, diver, datetime(2026, 6, 10, 0, 30, tzinfo=PLUS_7)),
            "late_minus_10": _dive(db, diver, datetime(2026, 6, 12, 23, 30, tzinfo=MINUS_10)),
            "unknown_offset": _dive(db, diver, datetime(2026, 6, 12, 15, 0, tzinfo=UTC), offset=None),
            "bare_date": _dive(db, diver, datetime(2026, 6, 12, tzinfo=UTC), offset=None, date_only=True),
            "the_9th": _on(db, diver, date(2026, 6, 9)),
            "the_13th": _on(db, diver, date(2026, 6, 13)),
        }

    def test_the_stored_instants_sit_where_the_cases_need_them(self, rows: dict[str, Dive]) -> None:
        assert rows["early_plus_7"].start_time.astimezone(UTC).date() == date(2026, 6, 9)
        assert rows["late_minus_10"].start_time.astimezone(UTC).date() == date(2026, 6, 13)

    @pytest.mark.asyncio
    async def test_the_sql_day_is_local_day_for_every_stored_state(
        self, async_db: AsyncSession, rows: dict[str, Dive]
    ) -> None:
        by_id = {dive.id: dive for dive in rows.values()}
        result = await async_db.execute(select(Dive.id, DIVE_LOCAL_DAY).where(Dive.id.in_(by_id)))

        days: dict[int, date] = dict(result.all())
        assert days == {id: local_day(dive.start_time, dive.utc_offset_minutes) for id, dive in by_id.items()}
        assert days[rows["bare_date"].id] == date(2026, 6, 12)

    @pytest.mark.asyncio
    async def test_a_dive_is_a_candidate_by_its_own_local_day(
        self, db: Session, async_db: AsyncSession, diver: User, rows: dict[str, Dive]
    ) -> None:
        trip = _trip(db, diver, JUNE)

        listed = set(await _uuids(async_db, diver, trip))

        inside = {"early_plus_7", "late_minus_10", "unknown_offset", "bare_date"}
        assert listed == {str(rows[name].uuid) for name in inside}
        assert await _counts(async_db, diver, trip) == (4, [4])


@pytest.mark.skipif(not db_available(), reason="No database connection available")
@pytest.mark.usefixtures("serving_cache")
class TestWhichPartsCover:
    @pytest.mark.asyncio
    async def test_an_open_end_reaches_every_later_day(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        trip = _trip(db, diver, (date(2026, 6, 10), None))
        later = _on(db, diver, date(2027, 6, 10))
        _on(db, diver, date(2026, 6, 9))

        assert await _uuids(async_db, diver, trip) == [str(later.uuid)]

    @pytest.mark.asyncio
    async def test_a_gap_between_parts_is_nobody_s_and_each_part_counts_its_own(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        trip = _trip(db, diver, JUNE, (date(2026, 6, 15), date(2026, 6, 17)))
        _on(db, diver, date(2026, 6, 13))
        _on(db, diver, date(2026, 6, 16))
        _on(db, diver, date(2026, 6, 11))
        _on(db, diver, date(2026, 6, 12))

        assert await _counts(async_db, diver, trip) == (3, [2, 1])

    @pytest.mark.asyncio
    async def test_a_dated_part_takes_its_days_from_an_open_ended_one(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        trip = _trip(db, diver, (date(2026, 6, 10), None), (date(2026, 6, 15), date(2026, 6, 17)))
        _on(db, diver, date(2026, 6, 16))

        assert await _counts(async_db, diver, trip) == (1, [0, 1])

    @pytest.mark.asyncio
    async def test_a_dive_on_a_trip_or_deleted_or_someone_else_s_is_no_candidate(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        trip, elsewhere = _trip(db, diver, JUNE), _trip(db, diver, JUNE)
        own = _on(db, diver, date(2026, 6, 11), trip=trip)
        _on(db, diver, date(2026, 6, 11), trip=elsewhere)
        _on(db, diver, date(2026, 6, 11), is_deleted=True)
        _on(db, other_diver, date(2026, 6, 11))
        candidate = _on(db, diver, date(2026, 6, 10))

        assert await _uuids(async_db, diver, trip) == [str(own.uuid), str(candidate.uuid)]
        assert await _counts(async_db, diver, trip) == (1, [1])

    @pytest.mark.asyncio
    async def test_a_dive_two_trips_cover_is_a_candidate_of_both(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """A past trip whose end was never filled in, and the later one covering the day."""
        stale, current = _trip(db, diver, (date(2026, 1, 1), None)), _trip(db, diver, JUNE)
        _on(db, diver, date(2026, 6, 11))

        assert await _counts(async_db, diver, stale) == (1, [1])
        assert await _counts(async_db, diver, current) == (1, [1])

    @pytest.mark.asyncio
    async def test_a_trip_whose_parts_carry_no_dates_has_none(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        trip = _trip(db, diver, (None, None))
        _on(db, diver, date(2026, 6, 11))

        assert await _uuids(async_db, diver, trip) == []
        assert await _counts(async_db, diver, trip) == (0, [0])


# opendiving-web's `src/lib/trip-dive-sections.test.ts` places the trip page's dives by the
# rule `part_for_day` mirrors (`tripPartForDay`). These are its cases, copied as
# (parts, local day, the part it is placed in): a change to the rule there has to be made here
# too, since nothing runs across the two repositories.
Dated = list[tuple[date | None, date | None]]

PLACEMENT_CASES: dict[str, tuple[Dated, dict[date, int]]] = {
    "gives a part with both dates its days over an open-ended one": (
        [(date(2026, 9, 1), None), (date(2026, 9, 10), date(2026, 9, 12))],
        {date(2026, 9, 11): 1, date(2026, 9, 13): 0},
    ),
    "meets in the middle between a part dated from its start and one dated to its end": (
        [(date(2021, 3, 20), None), (None, date(2021, 4, 6))],
        {date(2021, 3, 25): 0, date(2021, 4, 2): 1, date(2021, 3, 19): 1, date(2021, 4, 7): 0},
    ),
    "gives a day two parts cover to the first of them": (
        [(date(2026, 4, 3), date(2026, 4, 8)), (date(2026, 4, 5), date(2026, 4, 11))],
        {date(2026, 4, 11): 1, date(2026, 4, 6): 0, date(2026, 4, 4): 0, date(2026, 4, 9): 1},
    ),
    "gives a day two parts reach equally to the first of them": (
        [(date(2026, 4, 3), date(2026, 4, 8)), (date(2026, 4, 8), date(2026, 4, 12))],
        {date(2026, 4, 8): 0},
    ),
}


@pytest.mark.skipif(not db_available(), reason="No database connection available")
@pytest.mark.usefixtures("serving_cache")
class TestTheWebsPlacement:
    @pytest.mark.parametrize("case", PLACEMENT_CASES)
    @pytest.mark.asyncio
    async def test_each_day_is_attributed_where_the_page_places_it(
        self, db: Session, async_db: AsyncSession, diver: User, case: str
    ) -> None:
        dated, expected = PLACEMENT_CASES[case]
        parts = [TripPartRead(start_date=start, end_date=end) for start, end in dated]
        dives = {day: _on(db, diver, day) for day in expected}

        result = await async_db.execute(
            select(Dive.id, part_for_day(parts)).where(Dive.id.in_([dive.id for dive in dives.values()]))
        )

        attributed: dict[int, int | None] = dict(result.all())
        assert {day: attributed[dive.id] for day, dive in dives.items()} == expected

    @pytest.mark.parametrize("case", PLACEMENT_CASES)
    @pytest.mark.asyncio
    async def test_the_parts_counts_follow_the_placement(
        self, db: Session, async_db: AsyncSession, diver: User, case: str
    ) -> None:
        dated, expected = PLACEMENT_CASES[case]
        trip = _trip(db, diver, *dated)
        for day in expected:
            _on(db, diver, day)

        held = [list(expected.values()).count(index) for index in range(len(dated))]
        assert await _counts(async_db, diver, trip) == (len(expected), held)

    @pytest.mark.asyncio
    async def test_the_second_of_two_parts_with_the_same_dates_counts_nothing(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        trip = _trip(db, diver, JUNE, JUNE)
        _on(db, diver, date(2026, 6, 11))

        assert await _counts(async_db, diver, trip) == (1, [1, 0])


@pytest.mark.skipif(not db_available(), reason="No database connection available")
@pytest.mark.usefixtures("serving_cache")
class TestTheList:
    @pytest.mark.asyncio
    async def test_pages_the_trip_s_dives_and_its_candidates_newest_first(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        trip = _trip(db, diver, JUNE)
        first = _dive(db, diver, datetime(2026, 6, 10, 9, tzinfo=UTC), trip=trip)
        second = _dive(db, diver, datetime(2026, 6, 10, 14, tzinfo=UTC))
        third = _dive(db, diver, datetime(2026, 6, 11, 9, tzinfo=UTC), trip=trip)
        fourth = _dive(db, diver, datetime(2026, 6, 12, 9, tzinfo=UTC))
        # On the trip, outside every part: a trip dive is listed whatever its day.
        outside = _dive(db, diver, datetime(2026, 6, 20, 9, tzinfo=UTC), trip=trip)
        _on(db, diver, date(2026, 6, 21))

        first_page = await _listed(async_db, diver, trip, page=1, size=3)
        second_page = await _listed(async_db, diver, trip, page=2, size=3)

        assert (first_page["total_count"], first_page["has_more"], second_page["has_more"]) == (5, True, False)
        rows = first_page["data"] + second_page["data"]
        assert [row["uuid"] for row in rows] == [str(d.uuid) for d in (outside, fourth, third, second, first)]
        assert [row["trip_uuid"] for row in rows] == [str(trip.uuid), None, str(trip.uuid), None, str(trip.uuid)]

    @pytest.mark.asyncio
    async def test_someone_else_s_trip_is_a_404(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        trip = _trip(db, diver, JUNE)

        with pytest.raises(NotFoundException):
            await _listed(async_db, other_diver, trip)

    @pytest.mark.asyncio
    async def test_editing_a_part_s_dates_changes_what_it_lists(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """Through a cache that serves hits: the list is cached under the dives prefix, and a
        trip edit names no dive."""
        trip = _trip(db, diver, JUNE)
        widened_onto = _on(db, diver, date(2026, 6, 13))
        assert await _uuids(async_db, diver, trip) == []

        await trips_module.patch_trip(
            request=MagicMock(),
            uuid=trip.uuid,
            values=TripUpdateRequest(parts=[TripPartInput(start_date=date(2026, 6, 10), end_date=date(2026, 6, 13))]),
            current_user=_as(diver),
            db=async_db,
        )

        assert await _uuids(async_db, diver, trip) == [str(widened_onto.uuid)]


@pytest.mark.skipif(not db_available(), reason="No database connection available")
@pytest.mark.usefixtures("serving_cache")
class TestTheContacts:
    @pytest.mark.asyncio
    async def test_each_once_newest_dive_first_and_only_the_trip_s_own(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        trip = _trip(db, diver, JUNE)
        older, newer, a_candidate_s = (create_contact(db, diver) for _ in range(3))
        _on(db, diver, date(2026, 6, 10), trip=trip, contact_id=older.id)
        _on(db, diver, date(2026, 6, 11), trip=trip, contact_id=newer.id)
        _on(db, diver, date(2026, 6, 9), trip=trip, contact_id=older.id)
        _on(db, diver, date(2026, 6, 12), trip=trip)
        _on(db, diver, date(2026, 6, 12), contact_id=a_candidate_s.id)
        _on(db, diver, date(2026, 6, 13), trip=trip, contact_id=older.id, is_deleted=True)

        assert (await _read(async_db, diver, trip))["contact_uuids"] == [str(newer.uuid), str(older.uuid)]


@pytest.mark.skipif(not db_available(), reason="No database connection available")
@pytest.mark.usefixtures("serving_cache")
class TestAddingThem:
    @pytest.mark.asyncio
    async def test_a_named_dive_that_is_no_candidate_is_skipped_not_refused(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        trip, elsewhere = _trip(db, diver, JUNE), _trip(db, diver, JUNE)
        candidate = _on(db, diver, date(2026, 6, 11))
        on_a_trip = _on(db, diver, date(2026, 6, 11), trip=elsewhere)
        deleted = _on(db, diver, date(2026, 6, 11), is_deleted=True)
        someone_else_s = _on(db, other_diver, date(2026, 6, 11))
        assert await _counts(async_db, diver, trip) == (1, [1])

        named = [candidate, on_a_trip, deleted, someone_else_s]
        assert await _add(async_db, diver, trip, {"dive_uuids": [str(d.uuid) for d in named]}) == 1

        for dive in named:
            db.refresh(dive)
        assert [dive.trip_id for dive in named] == [trip.id, elsewhere.id, None, None]
        assert await _counts(async_db, diver, trip) == (0, [0])
        assert (await _read(async_db, diver, trip))["dive_count"] == 1

    @pytest.mark.asyncio
    async def test_a_part_named_by_its_dates_takes_its_own_and_no_other(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        trip = _trip(db, diver, (date(2026, 6, 10), None), (date(2026, 6, 15), date(2026, 6, 17)))
        mine = [_on(db, diver, date(2026, 6, 11)), _on(db, diver, date(2026, 6, 20))]
        the_other_part_s = _on(db, diver, date(2026, 6, 16))
        assert await _counts(async_db, diver, trip) == (3, [2, 1])

        assert await _add(async_db, diver, trip, {"part": {"start_date": "2026-06-10"}}) == 2

        assert await _counts(async_db, diver, trip) == (1, [0, 1])
        assert await _uuids(async_db, diver, trip) == [str(d.uuid) for d in (mine[1], the_other_part_s, mine[0])]
        assert [row["trip_uuid"] for row in (await _listed(async_db, diver, trip))["data"]] == [
            str(trip.uuid),
            None,
            str(trip.uuid),
        ]

    @pytest.mark.asyncio
    async def test_dates_no_part_carries_are_a_422(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        trip = _trip(db, diver, JUNE)
        _on(db, diver, date(2026, 6, 11))

        with pytest.raises(UnprocessableEntityException):
            await _add(async_db, diver, trip, {"part": {"start_date": "2026-06-10"}})

    @pytest.mark.asyncio
    async def test_naming_nothing_adds_every_candidate(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        trip = _trip(db, diver, JUNE, (date(2026, 6, 15), date(2026, 6, 17)))
        for day in (11, 12, 16):
            _on(db, diver, date(2026, 6, day))
        _on(db, diver, date(2026, 6, 13))

        assert await _add(async_db, diver, trip, {}) == 3

        assert await _counts(async_db, diver, trip) == (0, [0, 0])
        assert (await _read(async_db, diver, trip))["dive_count"] == 3

    @pytest.mark.asyncio
    async def test_someone_else_s_trip_is_a_404(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        trip = _trip(db, diver, JUNE)
        theirs = _on(db, other_diver, date(2026, 6, 11))

        with pytest.raises(NotFoundException):
            await _add(async_db, other_diver, trip, {})

        db.refresh(theirs)
        assert theirs.trip_id is None
