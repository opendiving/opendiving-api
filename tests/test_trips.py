"""Unit tests for the trip endpoints (`api/v1/trips.py`), the request schemas behind them
(`schemas/trip.py`) and the list query (`crud/crud_trips.py`), all of it about the parts a
trip is made of.

A trip used to be one date range plus an ordered list of placeless names. It is now an
ordered list of parts, each with its own optional range and its own optional place, which
moved five things that nothing else pins:

* a part's place is a *value object* validated on the way in - half a coordinate pair, a
  partial bounding box or a box with no position are refused rather than stored as a
  place a map cannot draw;
* `parts` is replaced wholesale, so `PATCH` has to tell no parts to write (leave them
  alone) from an empty list (clear them);
* a parts-only edit changes what the *list* pages say while leaving `update_data` empty,
  so the list cache has to be invalidated on a branch the route could easily skip;
* search moved from two columns of one table to a name-OR-EXISTS over the child table,
  which is why `test_picker_search.py` no longer has Trip in it;
* the list's ordering is an aggregate over that child table rather than a column, and a
  trip whose parts carry no dates has to sort last rather than first.

Mostly without a database: the route's collaborators are stubbed and the assertions are
on what it hands them (the `test_dive_update.py` style), and the search clause is asserted
as compiled SQL like `test_picker_search.py` does for the others.

The last three classes are the exception and run against a live Postgres, for the reason
`test_dive_neighbors.py` gives for its own split - what they have to get right is what the
*database* does, and a mocked session would only assert that we called the calls we called.
`TestReplaceParts` covers a wholesale replace, which is a `DELETE` plus re-numbered
inserts; `TestSearchAgainstPostgres` covers the correlated EXISTS, which compiles to the
same text whether or not it correlates; and `TestListOrdering` covers the correlated
aggregate the page is sorted by, which no compiled query can be read for. All three skip
themselves when no database is reachable; see CONTRIBUTING.md for why a run on the host
needs `POSTGRES_SERVER=localhost` to make them execute.
"""

import uuid as uuid_pkg
from datetime import UTC, date, datetime
from fnmatch import fnmatch
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1 import trips as trips_module
from src.app.core.exceptions.http_exceptions import NotFoundException, UnprocessableEntityException
from src.app.core.schemas import DATE_RANGE_MESSAGE
from src.app.core.utils import cache as cache_module
from src.app.core.utils.cache import across_builds, namespaced
from src.app.crud import crud_trips as crud_trips_module
from src.app.crud.crud_trip_parts import get_parts_for_trip, replace_parts_for_trip
from src.app.crud.crud_trips import get_trips_page
from src.app.models.trip import Trip
from src.app.models.trip_part import TripPart
from src.app.models.user import User
from src.app.schemas.location import (
    BBOX_MESSAGE,
    BBOX_NEEDS_COORDINATES_MESSAGE,
    BBOX_ORDER_MESSAGE,
    COORDINATE_PAIR_MESSAGE,
    LocationInput,
    LocationRead,
)
from src.app.schemas.trip import (
    MAX_TRIP_PARTS,
    TripCreate,
    TripPartInput,
    TripPartRead,
    TripReadInternal,
    TripUpdateRequest,
)
from tests.conftest import db_available
from tests.helpers.generators import create_trip

USER_ID = 1
USER_UUID = uuid7()

MOALBOAL = {
    "name": "Moalboal, Philippines",
    "full_name": "Moalboal, Cebu, Philippines",
    "latitude": 9.94,
    "longitude": 123.39,
    "bbox_south": 9.89,
    "bbox_north": 9.98,
    "bbox_west": 123.35,
    "bbox_east": 123.44,
}


def _internal_trip(trip_id: int = 11, name: str = "Cebu 2026") -> TripReadInternal:
    return TripReadInternal(
        id=trip_id,
        user_id=USER_ID,
        uuid=uuid7(),
        name=name,
        notes="",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _part(
    name: str | None = None,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    **location_fields: Any,
) -> TripPartRead:
    """A part, by the name of its place - `None` for one that has no place at all."""
    location = None if name is None else LocationRead(name=name, **location_fields)
    return TripPartRead(start_date=start_date, end_date=end_date, location=location)


def _as_sql(*conditions: Any) -> str:
    """The given `WHERE` clauses as literal Postgres SQL, for asserting on their shape."""
    return str(
        select(Trip.id)
        .where(*conditions)
        .compile(
            # `named` keeps literal `%` out of the printf-style escaping the default
            # `pyformat` paramstyle applies, which would double every one of them.
            dialect=postgresql.dialect(paramstyle="named"),
            compile_kwargs={"literal_binds": True},
        )
    )


class TestLocationInput:
    """What the schema refuses of a part's place. Every one of these would otherwise reach
    the table as a place the map cannot draw, and nothing downstream re-checks them."""

    def test_accepts_a_geocoded_place(self) -> None:
        location = LocationInput.model_validate(MOALBOAL)

        assert (location.latitude, location.bbox_east) == (9.94, 123.44)

    def test_accepts_a_bare_name(self) -> None:
        """The free-text escape hatch: a query the geocoder could not answer (or answered
        `[]` for while throttled) still has to be saveable, or an outage at the provider
        blocks the diver from recording where they went."""
        location = LocationInput.model_validate({"name": "Uncle Bert's house reef"})

        assert (location.latitude, location.longitude, location.full_name) == (None, None, None)

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            ({"name": "Moalboal", "latitude": 9.94}, COORDINATE_PAIR_MESSAGE),
            ({"name": "Moalboal", "longitude": 123.39}, COORDINATE_PAIR_MESSAGE),
            ({"name": "Moalboal", "latitude": 9.94, "longitude": None}, COORDINATE_PAIR_MESSAGE),
            ({**MOALBOAL, "bbox_east": None}, BBOX_MESSAGE),
            ({**MOALBOAL, "latitude": None, "longitude": None}, BBOX_NEEDS_COORDINATES_MESSAGE),
            ({**MOALBOAL, "bbox_south": 9.99}, BBOX_ORDER_MESSAGE),
        ],
    )
    def test_refuses_half_a_place(self, body: dict, message: str) -> None:
        with pytest.raises(ValidationError, match=message):
            LocationInput.model_validate(body)

    def test_accepts_a_box_that_crosses_the_antimeridian(self) -> None:
        """West > east is a real box, not a swapped pair - Nominatim returns those for
        Fiji and the Chukchi Sea. Ordering only means something north to south."""
        location = LocationInput.model_validate({**MOALBOAL, "bbox_west": 179.9, "bbox_east": -179.9})

        assert (location.bbox_west, location.bbox_east) == (179.9, -179.9)

    def test_refuses_a_field_the_api_does_not_have(self) -> None:
        """`extra="forbid"`, so a client sending the geocoder's raw row is told rather
        than having the half it does not recognize dropped on the floor."""
        with pytest.raises(ValidationError, match="extra_forbidden"):
            LocationInput.model_validate({**MOALBOAL, "osm_id": 12345})


class TestTripPartInput:
    """A part is dates and a place, and every combination of the two is legal except a
    reversed range."""

    def test_accepts_dates_and_a_place(self) -> None:
        """The ordinary part, and the only combination where both halves have to survive
        the same parse - the place nested rather than flattened beside the dates."""
        part = TripPartInput.model_validate(
            {"start_date": "2026-03-01", "end_date": "2026-03-05", "location": MOALBOAL}
        )

        assert part.location is not None
        assert (part.start_date, part.end_date, part.location.name, part.location.latitude) == (
            date(2026, 3, 1),
            date(2026, 3, 5),
            "Moalboal, Philippines",
            9.94,
        )

    def test_accepts_a_place_with_no_dates(self) -> None:
        """Every migrated middle part is this, and a diver can make one from the form."""
        part = TripPartInput.model_validate({"location": MOALBOAL})

        assert part.location is not None
        assert (part.location.name, part.start_date, part.end_date) == ("Moalboal, Philippines", None, None)

    def test_accepts_dates_with_no_place(self) -> None:
        """A transit day, or a week nobody ever geocoded - which is what every migrated
        trip with no location becomes."""
        part = TripPartInput.model_validate({"start_date": "2026-03-01"})

        assert (part.start_date, part.location) == (date(2026, 3, 1), None)

    def test_accepts_a_part_with_neither(self) -> None:
        assert TripPartInput.model_validate({}).location is None

    def test_refuses_a_reversed_range(self) -> None:
        with pytest.raises(ValidationError, match=DATE_RANGE_MESSAGE):
            TripPartInput.model_validate({"start_date": "2026-03-05", "end_date": "2026-03-01"})

    def test_equal_dates_are_a_one_day_part(self) -> None:
        part = TripPartInput.model_validate({"start_date": "2026-03-01", "end_date": "2026-03-01"})

        assert part.start_date == part.end_date

    def test_refuses_a_field_the_api_does_not_have(self) -> None:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            TripPartInput.model_validate({"start_date": "2026-03-01", "name": "Moalboal"})

    def test_bounds_how_many_parts_one_trip_can_have(self) -> None:
        body = {"name": "Everywhere 2026", "parts": [{"location": {"name": f"Stop {n}"}} for n in range(21)]}

        with pytest.raises(ValidationError, match="too_long"):
            TripCreate.model_validate(body)

    def test_the_bound_is_the_declared_one(self) -> None:
        """Pins the constant against the literal above, so raising one without the other
        fails here rather than silently in the web client's own cap."""
        assert MAX_TRIP_PARTS == 20

    def test_a_trip_with_no_parts_is_normal(self) -> None:
        trip = TripCreate.model_validate({"name": "Somewhere 2026"})

        assert trip.parts == []

    @pytest.mark.parametrize("member", ["start_date", "end_date", "locations"])
    @pytest.mark.parametrize("schema", [TripCreate, TripUpdateRequest])
    def test_a_trip_has_no_dates_or_places_of_its_own(self, schema: type[BaseModel], member: str) -> None:
        """Both write schemas are `extra="forbid"`, so a body spelling a trip the old way
        is a 422 rather than something quietly translated into parts."""
        with pytest.raises(ValidationError, match="extra_forbidden"):
            schema.model_validate({"name": "Cebu 2026", member: [] if member == "locations" else "2026-03-01"})


class TestPartsOnAnUpdate:
    """Leave them alone, clear them and replace them are three different instructions,
    and the value carries all three - `None`, `[]` and a list."""

    @pytest.mark.parametrize("body", [{"name": "Cebu 2026"}, {"parts": None}])
    def test_no_parts_to_write_is_not_an_instruction(self, body: dict[str, Any]) -> None:
        """An omitted key and an explicit null are the same instruction: leave them. Only
        `NON_NULLABLE_FIELDS` refuses a null, and `parts` is not one of them."""
        values = TripUpdateRequest.model_validate(body)

        assert values.parts is None

    def test_an_empty_list_clears_them(self) -> None:
        values = TripUpdateRequest.model_validate({"parts": []})

        assert values.parts == []

    def test_a_list_replaces_them(self) -> None:
        values = TripUpdateRequest.model_validate({"parts": [{"location": MOALBOAL}, {"location": {"name": "Bohol"}}]})

        assert values.parts is not None
        assert [part.location.name for part in values.parts if part.location] == ["Moalboal, Philippines", "Bohol"]


class _FakeRedis:
    """Enough of the client for `@cache` to run: every read misses, every write is recorded."""

    def __init__(self) -> None:
        self.written: list[str] = []

    async def get(self, key: str) -> None:
        return None

    async def set(self, key: str, value: str) -> None:
        self.written.append(key)

    async def expire(self, key: str, seconds: int) -> None:
        return None

    async def delete(self, key: str) -> None:
        return None


@pytest.fixture
def write_collaborators(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stubs everything `write_trip` and `patch_trip` touch, recording the calls.

    `patch_trip` carries a `@cache` decorator that drops `trip_cache:{uuid}` on the way
    out, so a client has to be in place or every call through it raises before reaching
    the body.
    """
    created = _internal_trip()
    stubs: dict[str, Any] = {
        "created": created,
        "create": AsyncMock(return_value=created),
        "get": AsyncMock(return_value=created),
        "update": AsyncMock(),
        "replace_parts": AsyncMock(),
        "get_parts": AsyncMock(return_value=[_part("Moalboal")]),
        "invalidate_list": AsyncMock(),
        "name_exists": AsyncMock(return_value=False),
        "owned": AsyncMock(return_value=created),
        "redis": _FakeRedis(),
    }

    monkeypatch.setattr(cache_module, "client", stubs["redis"])
    monkeypatch.setattr(trips_module.crud_trips, "create", stubs["create"])
    monkeypatch.setattr(trips_module.crud_trips, "get", stubs["get"])
    monkeypatch.setattr(trips_module.crud_trips, "update", stubs["update"])
    monkeypatch.setattr(trips_module, "replace_parts_for_trip", stubs["replace_parts"])
    monkeypatch.setattr(trips_module, "get_parts_for_trip", stubs["get_parts"])
    monkeypatch.setattr(trips_module, "trip_name_exists", stubs["name_exists"])
    monkeypatch.setattr(trips_module, "_get_owned_trip", stubs["owned"])
    monkeypatch.setattr(trips_module._trip_cache, "invalidate_list", stubs["invalidate_list"])

    return stubs


def _current_user() -> dict[str, Any]:
    return {"id": USER_ID, "uuid": USER_UUID}


async def _write(**body: Any) -> Any:
    trip = TripCreate.model_validate(body)
    return await trips_module.write_trip(request=MagicMock(), trip=trip, current_user=_current_user(), db=MagicMock())


async def _patch(values: dict[str, Any]) -> Any:
    return await trips_module.patch_trip(
        request=MagicMock(),
        uuid=uuid7(),
        values=TripUpdateRequest.model_validate(values),
        current_user=_current_user(),
        db=MagicMock(),
    )


def _written_parts(stubs: dict[str, Any]) -> list[TripPartInput]:
    parts: list[TripPartInput] = stubs["replace_parts"].await_args.kwargs["parts"]
    return parts


class TestWriteTrip:
    @pytest.mark.asyncio
    async def test_writes_the_parts_in_the_order_they_were_listed(self, write_collaborators: dict[str, Any]) -> None:
        await _write(name="Cebu 2026", parts=[{"location": MOALBOAL}, {"location": {"name": "Bohol"}}])

        assert write_collaborators["replace_parts"].await_args.kwargs["trip_id"] == write_collaborators["created"].id
        # Position is the index in the list the client sent, so the order it sent is the
        # order every read gives back - index 0 is what a single-part surface shows.
        assert [p.location.name for p in _written_parts(write_collaborators) if p.location] == [
            "Moalboal, Philippines",
            "Bohol",
        ]

    @pytest.mark.asyncio
    async def test_the_trip_row_never_sees_a_parts_key(self, write_collaborators: dict[str, Any]) -> None:
        """`TripCreateInternal` is `extra="forbid"` and a part is a row in another table,
        so only the trip's own columns may reach it."""
        await _write(name="Cebu 2026", parts=[{"location": MOALBOAL}])

        trip_internal = write_collaborators["create"].await_args.kwargs["object"]
        assert not hasattr(trip_internal, "parts")
        assert trip_internal.user_id == USER_ID

    @pytest.mark.asyncio
    async def test_the_response_carries_the_stored_parts(self, write_collaborators: dict[str, Any]) -> None:
        """Read back from the table rather than echoed from the request, so what the
        client renders is what a later `GET` will give it."""
        write_collaborators["get_parts"].return_value = [
            TripPartRead(
                start_date=date(2026, 3, 1),
                end_date=date(2026, 3, 5),
                location=LocationRead(name="Moalboal", latitude=9.94, longitude=123.39),
            )
        ]

        trip = await _write(name="Cebu 2026", parts=[{"location": MOALBOAL}])

        assert [part.location.name for part in trip.parts if part.location] == ["Moalboal"]
        assert trip.user_uuid == USER_UUID

    @pytest.mark.asyncio
    async def test_a_failed_part_write_rolls_back_and_is_a_422(self, write_collaborators: dict[str, Any]) -> None:
        """The trip row is already committed by this point, so the session has to be
        rolled back and the caller told - the alternative is a raw 500 over a session
        left in a failed state."""
        write_collaborators["replace_parts"].side_effect = IntegrityError("insert", {}, Exception("gone"))
        db = MagicMock()
        db.rollback = AsyncMock()
        trip = TripCreate.model_validate({"name": "Cebu 2026", "parts": [{"location": MOALBOAL}]})

        with pytest.raises(UnprocessableEntityException):
            await trips_module.write_trip(request=MagicMock(), trip=trip, current_user=_current_user(), db=db)

        db.rollback.assert_awaited_once()


class TestPatchTrip:
    @pytest.mark.asyncio
    async def test_an_omitted_key_leaves_the_parts_alone(self, write_collaborators: dict[str, Any]) -> None:
        await _patch({"name": "Cebu 2026"})

        write_collaborators["replace_parts"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_list_replaces_them_in_order(self, write_collaborators: dict[str, Any]) -> None:
        await _patch({"parts": [{"location": {"name": "Bohol"}}, {"location": MOALBOAL}]})

        assert [p.location.name for p in _written_parts(write_collaborators) if p.location] == [
            "Bohol",
            "Moalboal, Philippines",
        ]

    @pytest.mark.asyncio
    async def test_an_empty_list_clears_them(self, write_collaborators: dict[str, Any]) -> None:
        await _patch({"parts": []})

        assert _written_parts(write_collaborators) == []

    @pytest.mark.asyncio
    async def test_a_parts_only_edit_writes_no_trip_columns(self, write_collaborators: dict[str, Any]) -> None:
        """`update_data` is empty here, and an `UPDATE` with nothing to set is an error in
        FastCRUD rather than a no-op."""
        await _patch({"parts": [{"location": MOALBOAL}]})

        write_collaborators["update"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_parts_only_edit_still_invalidates_the_list(self, write_collaborators: dict[str, Any]) -> None:
        """The branch easiest to get wrong. The decorator on this route drops
        `trip_cache:{uuid}` only, so without this the trips list would go on showing the
        old parts for a minute after the edit - and the list is *ordered* by them now."""
        await _patch({"parts": [{"location": MOALBOAL}]})

        write_collaborators["invalidate_list"].assert_awaited_once_with(USER_ID)

    @pytest.mark.asyncio
    async def test_an_edit_that_changes_nothing_touches_nothing(self, write_collaborators: dict[str, Any]) -> None:
        await _patch({})

        write_collaborators["update"].assert_not_awaited()
        write_collaborators["replace_parts"].assert_not_awaited()
        write_collaborators["invalidate_list"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_reversed_range_on_one_part_is_refused(self) -> None:
        """The whole of the date enforcement now: there is no merged re-check, because a
        PATCH replaces a trip's parts wholesale rather than moving one date over a stored
        other. The message is the one a reversed trip range used to give."""
        with pytest.raises(ValidationError, match=DATE_RANGE_MESSAGE):
            TripUpdateRequest.model_validate({"parts": [{"start_date": "2026-03-05", "end_date": "2026-03-01"}]})

    @pytest.mark.asyncio
    async def test_a_failed_part_write_rolls_back_and_is_a_422(self, write_collaborators: dict[str, Any]) -> None:
        write_collaborators["replace_parts"].side_effect = IntegrityError("insert", {}, Exception("gone"))
        db = MagicMock()
        db.rollback = AsyncMock()

        with pytest.raises(UnprocessableEntityException):
            await trips_module.patch_trip(
                request=MagicMock(),
                uuid=uuid7(),
                values=TripUpdateRequest.model_validate({"parts": [{"location": MOALBOAL}]}),
                current_user=_current_user(),
                db=db,
            )

        db.rollback.assert_awaited_once()


def _get_request() -> MagicMock:
    request = MagicMock()
    request.method = "GET"
    return request


class TestReadPath:
    """The trips reads are hand-rolled `@cache` helpers rather than
    `OwnedResourceCache.read_list`/`read_item`, because each one zips a second query's
    rows back into the response. The keys have to stay byte-identical to the ones the
    write paths sweep, or an edit stops being visible until the entry expires.
    """

    @pytest.mark.asyncio
    async def test_a_page_embeds_each_trips_own_parts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = [
            {**_internal_trip(11, "Cebu 2026").model_dump(), "id": 11, "user_id": USER_ID},
            {**_internal_trip(12, "Red Sea 2025").model_dump(), "id": 12, "user_id": USER_ID},
        ]
        monkeypatch.setattr(trips_module, "get_trips_page", AsyncMock(return_value={"data": rows, "total_count": 2}))
        monkeypatch.setattr(
            trips_module,
            "get_parts_for_trips",
            AsyncMock(return_value={11: [_part("Moalboal"), _part("Bohol")], 12: []}),
        )

        with patch.object(cache_module, "client", _FakeRedis()):
            page = await trips_module._cached_read_trips(
                _get_request(),
                user_id=USER_ID,
                user_uuid=USER_UUID,
                db=MagicMock(),
                page=1,
                items_per_page=10,
                search=None,
            )

        assert [[part["location"]["name"] for part in trip["parts"]] for trip in page["data"]] == [
            ["Moalboal", "Bohol"],
            [],
        ]
        # The internal id the child rows hang off is not part of the public shape.
        assert "id" not in page["data"][0]

    @pytest.mark.asyncio
    async def test_the_search_term_reaches_the_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One hand-written query serves both branches now, so what used to be a choice
        between `get_multi` and `search_multi` is a parameter."""
        page_query = AsyncMock(return_value={"data": [], "total_count": 0})
        monkeypatch.setattr(trips_module, "get_trips_page", page_query)
        monkeypatch.setattr(trips_module, "get_parts_for_trips", AsyncMock(return_value={}))

        with patch.object(cache_module, "client", _FakeRedis()):
            await trips_module._cached_read_trips(
                _get_request(),
                user_id=USER_ID,
                user_uuid=USER_UUID,
                db=MagicMock(),
                page=2,
                items_per_page=10,
                search="moalboal",
            )

        page_query.assert_awaited_once()
        kwargs = page_query.await_args.kwargs  # type: ignore[union-attr]
        assert (kwargs["search"], kwargs["user_id"], kwargs["offset"], kwargs["limit"]) == (
            "moalboal",
            USER_ID,
            10,
            10,
        )

    @pytest.mark.asyncio
    async def test_the_list_key_is_the_one_writes_sweep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(trips_module, "get_trips_page", AsyncMock(return_value={"data": [], "total_count": 0}))
        monkeypatch.setattr(trips_module, "get_parts_for_trips", AsyncMock(return_value={}))
        redis = _FakeRedis()

        with patch.object(cache_module, "client", redis):
            await trips_module._cached_read_trips(
                _get_request(),
                user_id=USER_ID,
                user_uuid=USER_UUID,
                db=MagicMock(),
                page=2,
                items_per_page=10,
                search="moalboal",
            )

        # Byte-identical to what `OwnedResourceCache.read_list` wrote before the trips
        # reads were hand-rolled, and inside the `user_{id}_trips:*` pattern
        # `invalidate_list` sweeps after every create, update and delete.
        (key,) = redis.written
        assert key == namespaced(f"user_{USER_ID}_trips:page_2:items_per_page:10:search:moalboal:{USER_ID}")
        assert fnmatch(key, across_builds(f"user_{USER_ID}_trips:*"))

    @pytest.mark.asyncio
    async def test_a_single_trip_embeds_its_parts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        trip = _internal_trip()
        monkeypatch.setattr(trips_module.crud_trips, "get", AsyncMock(return_value=trip))
        monkeypatch.setattr(
            trips_module, "get_parts_for_trip", AsyncMock(return_value=[_part("Moalboal"), _part("Bohol")])
        )
        redis = _FakeRedis()

        with patch.object(cache_module, "client", redis):
            # Annotated `Any` because what comes back is what the client will get: on a
            # GET the decorator returns the round-tripped JSON, not the `TripRead` the
            # signature promises.
            body: Any = await trips_module._cached_read_trip(
                _get_request(), uuid=trip.uuid, owner_uuid=USER_UUID, db=MagicMock()
            )

        assert [part["location"]["name"] for part in body["parts"]] == ["Moalboal", "Bohol"]
        # The same key `patch_trip` and `erase_trip` delete - moving off
        # `OwnedResourceCache.read_item` had to leave invalidation untouched.
        assert list(redis.written) == [namespaced(f"trip_cache:{trip.uuid}")]

    @pytest.mark.asyncio
    async def test_a_missing_trip_is_a_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(trips_module.crud_trips, "get", AsyncMock(return_value=None))

        with patch.object(cache_module, "client", _FakeRedis()), pytest.raises(NotFoundException):
            await trips_module._cached_read_trip(
                _get_request(), uuid=uuid_pkg.UUID(int=0), owner_uuid=USER_UUID, db=MagicMock()
            )


class TestSearchConditions:
    """What replaced the two-column `search_clause` when `trip.location` was dropped -
    see the note left in `test_picker_search.py`, which used to pin Trip to
    `("name", "location")`."""

    def test_matches_the_name_or_any_of_the_places_it_went_to(self) -> None:
        sql = _as_sql(*crud_trips_module.search_conditions(user_id=USER_ID, term="moalboal"))

        # OR'd across the two tables: a trip called "Moalboal 2026" and one merely *named
        # after Cebu* that went there both have to match.
        assert "trip.name ILIKE '%moalboal%'" in sql
        assert "EXISTS" in sql
        assert "trip_part.name ILIKE '%moalboal%'" in sql
        # The fuller name too, so "cebu" finds a trip whose places stop at the country.
        assert "trip_part.full_name ILIKE '%moalboal%'" in sql

    def test_the_exists_is_correlated_to_the_trip_being_matched(self) -> None:
        """Without the correlation every trip matches as soon as *any* trip in the table
        has a part with the term in it."""
        sql = _as_sql(*crud_trips_module.search_conditions(user_id=USER_ID, term="moalboal"))

        assert "trip_part.trip_id = trip.id" in sql

    def test_stays_inside_the_callers_own_trips(self) -> None:
        sql = _as_sql(*crud_trips_module.search_conditions(user_id=7, term="moalboal"))

        assert "trip.user_id = 7" in sql
        # Ownership is the whole scope now: trips are hard-deleted, so there is no
        # liveness clause the EXISTS could be written outside of.
        assert "is_deleted" not in sql

    def test_a_wildcard_in_the_term_matches_literally(self) -> None:
        """`escape_like` on both sides of the OR, same as every other picker - otherwise
        a search for "50%" is a search for everything, in the child table too."""
        sql = _as_sql(*crud_trips_module.search_conditions(user_id=USER_ID, term="50%"))

        # The rendered literal doubles each backslash; what matters is that the `%` the
        # diver typed arrives escaped rather than as a live wildcard, under an `ESCAPE`.
        assert sql.count(f"'%50{'\\' * 2}%%' ESCAPE") == 3


@pytest.fixture
def trip(db: Session, diver: User) -> Trip:
    return create_trip(db, diver)


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestReplaceParts:
    """`replace_parts_for_trip` against a live Postgres - what the table holds after a
    write, which is the half a stubbed session cannot answer.

    A trip's parts are replaced wholesale rather than diffed, so every edit is a `DELETE`
    plus inserts numbered from the *new* list. Both halves fail quietly if they regress: a
    lost `DELETE` leaves the surplus rows of the previous, longer list behind, and
    positions carried over from it re-order the page the next time it is read. Either one
    looks correct until a trip is edited twice.
    """

    @staticmethod
    def _inputs(*names: str) -> list[TripPartInput]:
        return [TripPartInput(location=LocationInput(name=name)) for name in names]

    @staticmethod
    def _rows(db: Session, trip: Trip) -> list[tuple[str | None, int]]:
        rows = db.query(TripPart).filter(TripPart.trip_id == trip.id).order_by(TripPart.id).all()
        return [(row.name, row.position) for row in rows]

    @pytest.fixture(autouse=True)
    def _no_seeded_part(self, db: Session, async_db: AsyncSession, trip: Trip) -> None:
        """`create_trip` gives its trip one dated part, which every assertion below would
        otherwise have to account for."""
        db.query(TripPart).filter(TripPart.trip_id == trip.id).delete()
        db.commit()

    @pytest.mark.asyncio
    async def test_position_is_the_index_in_the_list_that_was_sent(
        self, db: Session, async_db: AsyncSession, trip: Trip
    ) -> None:
        await replace_parts_for_trip(db=async_db, trip_id=trip.id, parts=self._inputs("Moalboal", "Bohol"))

        assert self._rows(db, trip) == [("Moalboal", 0), ("Bohol", 1)]

    @pytest.mark.asyncio
    async def test_a_shorter_list_leaves_none_of_the_longer_one_behind(
        self, db: Session, async_db: AsyncSession, trip: Trip
    ) -> None:
        """The surplus rows have to go, and the survivor has to be re-numbered from the
        new list - a "Bohol" still sitting at position 1 would read back as a second
        part the diver deleted."""
        await replace_parts_for_trip(db=async_db, trip_id=trip.id, parts=self._inputs("Moalboal", "Bohol", "Anilao"))

        await replace_parts_for_trip(db=async_db, trip_id=trip.id, parts=self._inputs("Bohol"))

        assert self._rows(db, trip) == [("Bohol", 0)]

    @pytest.mark.asyncio
    async def test_an_empty_list_clears_them(self, db: Session, async_db: AsyncSession, trip: Trip) -> None:
        await replace_parts_for_trip(db=async_db, trip_id=trip.id, parts=self._inputs("Moalboal"))

        await replace_parts_for_trip(db=async_db, trip_id=trip.id, parts=[])

        assert self._rows(db, trip) == []

    @pytest.mark.asyncio
    async def test_duplicate_names_are_legal(self, db: Session, async_db: AsyncSession, trip: Trip) -> None:
        """Two stays in the same town on one trip. There is no unique constraint, and
        `position` is the only thing telling the rows apart - which is why the read is
        ordered by it and nothing dedupes."""
        await replace_parts_for_trip(db=async_db, trip_id=trip.id, parts=self._inputs("Dahab", "Dahab"))

        assert self._rows(db, trip) == [("Dahab", 0), ("Dahab", 1)]

    @pytest.mark.asyncio
    async def test_a_part_with_no_place_stores_a_null_name(
        self, db: Session, async_db: AsyncSession, trip: Trip
    ) -> None:
        """The column that had to become nullable, and the one thing that says whether a
        row has a place at all - `get_parts_for_trip` reads back `location=None` off it."""
        await replace_parts_for_trip(db=async_db, trip_id=trip.id, parts=[TripPartInput(start_date=date(2026, 3, 1))])

        assert self._rows(db, trip) == [(None, 0)]
        (read_back,) = await get_parts_for_trip(db=async_db, trip_id=trip.id)
        assert (read_back.location, read_back.start_date) == (None, date(2026, 3, 1))

    @pytest.mark.asyncio
    async def test_another_trips_parts_are_left_alone(
        self, db: Session, async_db: AsyncSession, diver: User, trip: Trip
    ) -> None:
        """The `DELETE` is scoped by `trip_id`, and a suite with one trip in it cannot
        notice when that stops being true."""
        neighbour = Trip(user_id=diver.id, name=f"Elsewhere {uuid7().hex[-8:]}", notes="")
        db.add(neighbour)
        db.commit()
        await replace_parts_for_trip(db=async_db, trip_id=neighbour.id, parts=self._inputs("Koh Tao"))

        await replace_parts_for_trip(db=async_db, trip_id=trip.id, parts=self._inputs("Moalboal"))

        assert self._rows(db, neighbour) == [("Koh Tao", 0)]

    @pytest.mark.asyncio
    async def test_what_was_written_is_what_reads_back(self, db: Session, async_db: AsyncSession, trip: Trip) -> None:
        """Round-trips every column, including the antimeridian-legal `west > east`."""
        location = LocationInput(**{**MOALBOAL, "bbox_west": 179.9, "bbox_east": -179.9})
        written = TripPartInput(start_date=date(2026, 3, 1), end_date=date(2026, 3, 12), location=location)

        await replace_parts_for_trip(db=async_db, trip_id=trip.id, parts=[written])

        (read_back,) = await get_parts_for_trip(db=async_db, trip_id=trip.id)
        assert read_back.model_dump() == written.model_dump()


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestListOrdering:
    """`get_trips_page` executed rather than compiled.

    The ordering is a correlated `min(trip_part.start_date)`, which no assertion on
    rendered SQL can settle: the aggregate has to be evaluated per trip, and the null
    placement only shows against rows. Getting it wrong puts every dateless trip - a
    legal state from this revision on - at the top of every diver's list.
    """

    @pytest_asyncio.fixture
    async def _seeded(self, db: Session, async_db: AsyncSession, diver: User) -> str:
        """Three trips: a recent one, an older one, and one with no dates anywhere. The
        older trip's *later* part is what proves the aggregate is a `min` rather than the
        first row by position."""
        tag = uuid7().hex[-8:]
        recent = Trip(user_id=diver.id, name=f"Recent {tag}", notes="")
        older = Trip(user_id=diver.id, name=f"Older {tag}", notes="")
        undated = Trip(user_id=diver.id, name=f"Undated {tag}", notes="")
        db.add_all([recent, older, undated])
        db.commit()
        db.add_all(
            [
                TripPart(trip_id=recent.id, position=0, start_date=date(2026, 6, 1)),
                TripPart(trip_id=older.id, position=0, start_date=date(2025, 3, 1)),
                TripPart(trip_id=older.id, position=1, start_date=date(2026, 9, 1)),
                TripPart(trip_id=undated.id, position=0, name=f"Nowhere {tag}"),
            ]
        )
        db.commit()
        return tag

    @staticmethod
    async def _names(db: AsyncSession, user_id: int) -> list[str]:
        page = await get_trips_page(db=db, user_id=user_id, offset=0, limit=50)
        return [row["name"] for row in page["data"]]

    @pytest.mark.asyncio
    async def test_the_earliest_part_places_the_trip_and_a_dateless_one_sorts_last(
        self, async_db: AsyncSession, diver: User, _seeded: str
    ) -> None:
        assert await self._names(async_db, diver.id) == [f"Recent {_seeded}", f"Older {_seeded}", f"Undated {_seeded}"]

    @pytest.mark.asyncio
    async def test_the_count_includes_the_dateless_trip(
        self, async_db: AsyncSession, diver: User, _seeded: str
    ) -> None:
        """`NULLS LAST` places it; nothing may filter it out. A diver whose only trip has
        no dates must still see it."""
        page = await get_trips_page(db=async_db, user_id=diver.id, offset=0, limit=50)

        assert page["total_count"] == len(page["data"]) == 3

    @pytest.mark.asyncio
    async def test_a_search_uses_the_same_ordering(self, async_db: AsyncSession, diver: User, _seeded: str) -> None:
        """One query serves both branches, which is the whole reason it is hand-written -
        the searched branch used to reach `search_multi`, which cannot ask for a null
        placement at all."""
        page = await get_trips_page(db=async_db, user_id=diver.id, offset=0, limit=50, search=_seeded)

        assert [row["name"] for row in page["data"]] == [
            f"Recent {_seeded}",
            f"Older {_seeded}",
            f"Undated {_seeded}",
        ]

    @pytest.mark.asyncio
    async def test_a_search_matches_a_part_that_only_has_a_place(
        self, async_db: AsyncSession, diver: User, _seeded: str
    ) -> None:
        page = await get_trips_page(db=async_db, user_id=diver.id, offset=0, limit=50, search=f"nowhere {_seeded}")

        assert [row["name"] for row in page["data"]] == [f"Undated {_seeded}"]

    @pytest.mark.asyncio
    async def test_another_divers_trips_are_never_listed(
        self, async_db: AsyncSession, other_diver: User, _seeded: str
    ) -> None:
        assert await self._names(async_db, other_diver.id) == []


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestSearchAgainstPostgres:
    """`search_conditions` executed rather than compiled.

    The compiled-SQL assertions above cannot settle the one failure that matters here:
    an EXISTS that does not correlate to the row being matched renders the same
    `trip_part.trip_id = trip.id` text and matches *every* trip as soon as any trip
    in the table has a part with the term in it. Only running it against rows tells
    the two apart - and getting it wrong would show one diver a page of trips they have
    no business seeing named after a place they never went.
    """

    @staticmethod
    async def _matching_names(db: AsyncSession, user_id: int, term: str) -> list[str]:
        rows = await db.execute(
            select(Trip.name).where(*crud_trips_module.search_conditions(user_id=user_id, term=term))
        )
        return sorted(name for (name,) in rows)

    @pytest_asyncio.fixture
    async def _seeded(self, db: Session, async_db: AsyncSession, diver: User) -> tuple[str, str]:
        """Two trips, one of which went to Moalboal - and neither of which is named it."""
        tag = uuid7().hex[-8:]
        went, stayed = (
            Trip(user_id=diver.id, name=f"Cebu {tag}", notes=""),
            Trip(user_id=diver.id, name=f"Elsewhere {tag}", notes=""),
        )
        db.add_all([went, stayed])
        db.commit()
        await replace_parts_for_trip(
            db=async_db,
            trip_id=went.id,
            parts=[
                TripPartInput(
                    location=LocationInput(name=f"Moalboal {tag}", full_name=f"Moalboal, Cebu, Philippines {tag}")
                )
            ],
        )
        return went.name, stayed.name

    @pytest.mark.asyncio
    async def test_only_the_trip_that_went_there_matches(
        self, async_db: AsyncSession, diver: User, _seeded: tuple[str, str]
    ) -> None:
        went, _ = _seeded
        tag = went.rsplit(" ", 1)[-1]

        assert await self._matching_names(async_db, diver.id, f"moalboal {tag}") == [went]

    @pytest.mark.asyncio
    async def test_the_fuller_name_matches_too(
        self, async_db: AsyncSession, diver: User, _seeded: tuple[str, str]
    ) -> None:
        """ "philippines" has to find a trip whose places are all named after towns - the
        member nothing renders is still the one a diver may remember.
        """
        went, _ = _seeded
        tag = went.rsplit(" ", 1)[-1]

        assert await self._matching_names(async_db, diver.id, f"philippines {tag}") == [went]

    @pytest.mark.asyncio
    async def test_another_divers_trips_are_never_matched(
        self, async_db: AsyncSession, other_diver: User, _seeded: tuple[str, str]
    ) -> None:
        went, _ = _seeded
        tag = went.rsplit(" ", 1)[-1]

        assert await self._matching_names(async_db, other_diver.id, f"moalboal {tag}") == []
