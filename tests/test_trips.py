"""Unit tests for the trip endpoints (`api/v1/trips.py`) and the request schemas behind
them (`schemas/trip.py`), all of it about the places a trip went to.

Those places used to be one free-text `trip.location` column and are now ordered rows in
`trip_location`, which moved four things that nothing else pins:

* a location is a *value object* validated on the way in - half a coordinate pair, a
  partial bounding box or a box with no position are refused rather than stored as a
  place a map cannot draw;
* `locations` is replaced wholesale, so `PATCH` has to tell an omitted key (leave them
  alone) from an empty list (clear them) - the same `model_fields_set` distinction
  `test_dive_update.py` covers for `trip_uuid`;
* a locations-only edit changes what the *list* pages say while leaving `update_data`
  empty, so the list cache has to be invalidated on a branch the route could easily
  skip;
* search moved from two columns of one table to a name-OR-EXISTS over the child table,
  which is why `test_picker_search.py` no longer has Trip in it.

One thing here is not about locations at all: `TestPatchTrip` also covers the trip's date
range, which `TripUpdate` can only check when both dates arrive together, so the route
re-checks the merged pair against the stored row.

Mostly without a database: the route's collaborators are stubbed and the assertions are
on what it hands them (the `test_dive_update.py` style), and the search clause is asserted
as compiled SQL like `test_picker_search.py` does for the others.

The last two classes are the exception and run against a live Postgres, for the reason
`test_dive_neighbors.py` gives for its own split - what they have to get right is what the
*database* does, and a mocked session would only assert that we called the calls we called.
`TestReplaceLocations` covers a wholesale replace, which is a `DELETE` plus re-numbered
inserts; `TestSearchAgainstPostgres` covers the correlated EXISTS, which compiles to the
same text whether or not it correlates. Both skip themselves when no database is
reachable; see CONTRIBUTING.md for why a run on the host needs `POSTGRES_SERVER=localhost`
to make them execute.
"""

import uuid as uuid_pkg
from datetime import UTC, date, datetime
from fnmatch import fnmatch
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from pydantic import ValidationError
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
from src.app.crud.crud_trip_locations import get_locations_for_trip, replace_locations_for_trip
from src.app.models.trip import Trip
from src.app.models.trip_location import TripLocation
from src.app.models.user import User
from src.app.schemas.dive_site import COORDINATE_PAIR_MESSAGE
from src.app.schemas.trip import (
    BBOX_MESSAGE,
    BBOX_NEEDS_COORDINATES_MESSAGE,
    BBOX_ORDER_MESSAGE,
    MAX_TRIP_LOCATIONS,
    TripCreate,
    TripLocationInput,
    TripLocationRead,
    TripReadInternal,
    TripUpdateRequest,
)
from tests.conftest import db_available
from tests.helpers.generators import create_trip

USER_ID = 1
USER_UUID = uuid7()

MOALBOAL = {
    "name": "Moalboal",
    "display_name": "Moalboal, Cebu, Philippines",
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
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 12),
        notes="",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _read(name: str, **overrides: Any) -> TripLocationRead:
    return TripLocationRead(name=name, **overrides)


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


class TestTripLocationInput:
    """What the schema refuses. Every one of these would otherwise reach the table as a
    place the map cannot draw, and nothing downstream re-checks them."""

    def test_accepts_a_geocoded_place(self) -> None:
        location = TripLocationInput.model_validate(MOALBOAL)

        assert (location.latitude, location.bbox_east) == (9.94, 123.44)

    def test_accepts_a_bare_name(self) -> None:
        """The free-text escape hatch: a query the geocoder could not answer (or answered
        `[]` for while throttled) still has to be saveable, or an outage at the provider
        blocks the diver from recording where they went."""
        location = TripLocationInput.model_validate({"name": "Uncle Bert's house reef"})

        assert (location.latitude, location.longitude, location.display_name) == (None, None, None)

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
            TripLocationInput.model_validate(body)

    def test_accepts_a_box_that_crosses_the_antimeridian(self) -> None:
        """West > east is a real box, not a swapped pair - Nominatim returns those for
        Fiji and the Chukchi Sea. Ordering only means something north to south."""
        location = TripLocationInput.model_validate({**MOALBOAL, "bbox_west": 179.9, "bbox_east": -179.9})

        assert (location.bbox_west, location.bbox_east) == (179.9, -179.9)

    def test_refuses_a_field_the_api_does_not_have(self) -> None:
        """`extra="forbid"`, so a client sending the geocoder's raw row is told rather
        than having the half it does not recognize dropped on the floor."""
        with pytest.raises(ValidationError, match="extra_forbidden"):
            TripLocationInput.model_validate({**MOALBOAL, "osm_id": 12345})

    def test_bounds_how_many_places_one_trip_can_have(self) -> None:
        body = {
            "name": "Everywhere 2026",
            "start_date": "2026-03-01",
            "locations": [{"name": f"Stop {n}"} for n in range(MAX_TRIP_LOCATIONS + 1)],
        }

        with pytest.raises(ValidationError, match="too_long"):
            TripCreate.model_validate(body)

    def test_a_trip_with_no_locations_is_normal(self) -> None:
        trip = TripCreate.model_validate({"name": "Somewhere 2026", "start_date": "2026-03-01"})

        assert trip.locations == []


class TestLocationsOnAnUpdate:
    """Omitted, `[]` and a list are three different instructions, and only
    `model_fields_set` tells the first two apart - `locations` is `None` either way."""

    def test_an_omitted_key_is_not_an_instruction(self) -> None:
        values = TripUpdateRequest.model_validate({"name": "Cebu 2026"})

        assert values.locations is None
        assert "locations" not in values.model_fields_set

    def test_an_empty_list_clears_them(self) -> None:
        values = TripUpdateRequest.model_validate({"locations": []})

        assert values.locations == []

    def test_a_list_replaces_them(self) -> None:
        values = TripUpdateRequest.model_validate({"locations": [MOALBOAL, {"name": "Bohol"}]})

        assert values.locations is not None
        assert [location.name for location in values.locations] == ["Moalboal", "Bohol"]


class _FakeRedis:
    """A cache that never hits, recording what the decorator writes and deletes."""

    def __init__(self) -> None:
        self.written: dict[str, str] = {}
        self.deleted: list[str] = []

    async def get(self, key: str) -> None:
        return None

    async def set(self, key: str, value: str) -> None:
        self.written[key] = value

    async def expire(self, key: str, seconds: int) -> None:
        return None

    async def delete(self, key: str) -> None:
        self.deleted.append(key)


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
        "replace_locations": AsyncMock(),
        "get_locations": AsyncMock(return_value=[_read("Moalboal")]),
        "invalidate_list": AsyncMock(),
        "name_exists": AsyncMock(return_value=False),
        "owned": AsyncMock(return_value=created),
        "redis": _FakeRedis(),
    }

    monkeypatch.setattr(cache_module, "client", stubs["redis"])
    monkeypatch.setattr(trips_module.crud_trips, "create", stubs["create"])
    monkeypatch.setattr(trips_module.crud_trips, "get", stubs["get"])
    monkeypatch.setattr(trips_module.crud_trips, "update", stubs["update"])
    monkeypatch.setattr(trips_module, "replace_locations_for_trip", stubs["replace_locations"])
    monkeypatch.setattr(trips_module, "get_locations_for_trip", stubs["get_locations"])
    monkeypatch.setattr(trips_module, "trip_name_exists", stubs["name_exists"])
    monkeypatch.setattr(trips_module, "_get_owned_trip", stubs["owned"])
    monkeypatch.setattr(trips_module._trip_cache, "invalidate_list", stubs["invalidate_list"])

    return stubs


def _current_user() -> dict[str, Any]:
    return {"id": USER_ID, "uuid": USER_UUID}


async def _write(**body: Any) -> Any:
    trip = TripCreate.model_validate({"start_date": "2026-03-01", **body})
    return await trips_module.write_trip(request=MagicMock(), trip=trip, current_user=_current_user(), db=MagicMock())


async def _patch(values: dict[str, Any]) -> Any:
    return await trips_module.patch_trip(
        request=MagicMock(),
        uuid=uuid7(),
        values=TripUpdateRequest.model_validate(values),
        current_user=_current_user(),
        db=MagicMock(),
    )


class TestWriteTrip:
    @pytest.mark.asyncio
    async def test_writes_the_locations_in_the_order_they_were_listed(
        self, write_collaborators: dict[str, Any]
    ) -> None:
        await _write(name="Cebu 2026", locations=[MOALBOAL, {"name": "Bohol"}])

        kwargs = write_collaborators["replace_locations"].await_args.kwargs
        assert kwargs["trip_id"] == write_collaborators["created"].id
        # Position is the index in the list the client sent, so the order it sent is the
        # order every read gives back - index 0 is what a single-location surface shows.
        assert [location.name for location in kwargs["locations"]] == ["Moalboal", "Bohol"]

    @pytest.mark.asyncio
    async def test_the_trip_row_never_sees_a_locations_key(self, write_collaborators: dict[str, Any]) -> None:
        """`TripCreateInternal` is `extra="forbid"` and `locations` is rows in another
        table, so the dump has to exclude it - forget the exclusion and every create is a
        500."""
        await _write(name="Cebu 2026", locations=[MOALBOAL])

        trip_internal = write_collaborators["create"].await_args.kwargs["object"]
        assert not hasattr(trip_internal, "locations")
        assert trip_internal.user_id == USER_ID

    @pytest.mark.asyncio
    async def test_the_response_carries_the_stored_locations(self, write_collaborators: dict[str, Any]) -> None:
        """Read back from the table rather than echoed from the request, so what the
        client renders is what a later `GET` will give it."""
        write_collaborators["get_locations"].return_value = [_read("Moalboal", latitude=9.94, longitude=123.39)]

        trip = await _write(name="Cebu 2026", locations=[MOALBOAL])

        assert [location.name for location in trip.locations] == ["Moalboal"]
        assert trip.user_uuid == USER_UUID

    @pytest.mark.asyncio
    async def test_a_failed_location_write_rolls_back_and_is_a_422(self, write_collaborators: dict[str, Any]) -> None:
        """The trip row is already committed by this point, so the session has to be
        rolled back and the caller told - the alternative is a raw 500 over a session
        left in a failed state."""
        write_collaborators["replace_locations"].side_effect = IntegrityError("insert", {}, Exception("gone"))
        db = MagicMock()
        db.rollback = AsyncMock()
        trip = TripCreate.model_validate({"name": "Cebu 2026", "start_date": "2026-03-01", "locations": [MOALBOAL]})

        with pytest.raises(UnprocessableEntityException):
            await trips_module.write_trip(request=MagicMock(), trip=trip, current_user=_current_user(), db=db)

        db.rollback.assert_awaited_once()


class TestPatchTrip:
    @pytest.mark.asyncio
    async def test_an_omitted_key_leaves_the_locations_alone(self, write_collaborators: dict[str, Any]) -> None:
        await _patch({"name": "Cebu 2026"})

        write_collaborators["replace_locations"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_list_replaces_them_in_order(self, write_collaborators: dict[str, Any]) -> None:
        await _patch({"locations": [{"name": "Bohol"}, MOALBOAL]})

        kwargs = write_collaborators["replace_locations"].await_args.kwargs
        assert [location.name for location in kwargs["locations"]] == ["Bohol", "Moalboal"]

    @pytest.mark.asyncio
    async def test_an_empty_list_clears_them(self, write_collaborators: dict[str, Any]) -> None:
        await _patch({"locations": []})

        assert write_collaborators["replace_locations"].await_args.kwargs["locations"] == []

    @pytest.mark.asyncio
    async def test_a_locations_only_edit_writes_no_trip_columns(self, write_collaborators: dict[str, Any]) -> None:
        """`update_data` is empty here, and an `UPDATE` with nothing to set is an error in
        FastCRUD rather than a no-op."""
        await _patch({"locations": [MOALBOAL]})

        write_collaborators["update"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_locations_only_edit_still_invalidates_the_list(self, write_collaborators: dict[str, Any]) -> None:
        """The branch easiest to get wrong. The decorator on this route drops
        `trip_cache:{uuid}` only, so without this the trips list would go on showing the
        old places for a minute after the edit."""
        await _patch({"locations": [MOALBOAL]})

        write_collaborators["invalidate_list"].assert_awaited_once_with(USER_ID)

    @pytest.mark.asyncio
    async def test_an_edit_that_changes_nothing_touches_nothing(self, write_collaborators: dict[str, Any]) -> None:
        await _patch({})

        write_collaborators["update"].assert_not_awaited()
        write_collaborators["replace_locations"].assert_not_awaited()
        write_collaborators["invalidate_list"].assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            # The stored trip runs 2026-03-01 to 2026-03-12. Either date alone can cross
            # the other, and `TripUpdate` sees only what was sent.
            pytest.param({"end_date": "2026-02-28"}, id="end_date-before-the-stored-start"),
            pytest.param({"start_date": "2026-03-13"}, id="start_date-after-the-stored-end"),
        ],
    )
    async def test_one_date_may_not_cross_the_stored_other(
        self, write_collaborators: dict[str, Any], body: dict[str, Any]
    ) -> None:
        """The gap this closes. `TripUpdate.check_date_range` only fires when both dates
        arrive together, and the `trip` table has no CHECK constraint, so a single-date
        PATCH used to write a trip that ends before it begins."""
        with pytest.raises(UnprocessableEntityException) as excinfo:
            await _patch(body)

        assert DATE_RANGE_MESSAGE in str(excinfo.value.detail)
        write_collaborators["update"].assert_not_awaited()
        write_collaborators["invalidate_list"].assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"end_date": "2026-03-13"}, id="end_date-after-the-stored-start"),
            # Equal dates are a one-day trip, not a reversed range.
            pytest.param({"end_date": "2026-03-01"}, id="end_date-on-the-stored-start"),
            pytest.param({"start_date": "2026-03-12"}, id="start_date-on-the-stored-end"),
            # `end_date` is the one nullable half: an open-ended trip is a real state, so
            # clearing it can never conflict with whatever start date is stored.
            pytest.param({"end_date": None}, id="end_date-cleared"),
        ],
    )
    async def test_a_date_that_still_orders_is_written(
        self, write_collaborators: dict[str, Any], body: dict[str, Any]
    ) -> None:
        await _patch(body)

        write_collaborators["update"].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_open_ended_trip_takes_any_start_date(self, write_collaborators: dict[str, Any]) -> None:
        """Nothing to cross when the stored `end_date` is null, so the merge must not read
        the missing half as a reason to refuse."""
        stored = _internal_trip()
        stored.end_date = None
        write_collaborators["owned"].return_value = stored

        await _patch({"start_date": "2030-01-01"})

        write_collaborators["update"].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_edit_that_names_no_date_is_never_range_checked(self, write_collaborators: dict[str, Any]) -> None:
        """The merged check is keyed off which keys were sent, not off the stored row, so
        a rename can never be refused for a range the caller did not touch."""
        stored = _internal_trip()
        stored.end_date = date(2020, 1, 1)
        write_collaborators["owned"].return_value = stored

        await _patch({"name": "Cebu 2026"})

        write_collaborators["update"].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_failed_location_write_rolls_back_and_is_a_422(self, write_collaborators: dict[str, Any]) -> None:
        write_collaborators["replace_locations"].side_effect = IntegrityError("insert", {}, Exception("gone"))
        db = MagicMock()
        db.rollback = AsyncMock()

        with pytest.raises(UnprocessableEntityException):
            await trips_module.patch_trip(
                request=MagicMock(),
                uuid=uuid7(),
                values=TripUpdateRequest.model_validate({"locations": [MOALBOAL]}),
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
    async def test_a_page_embeds_each_trips_own_locations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = [
            {**_internal_trip(11, "Cebu 2026").model_dump(), "id": 11},
            {**_internal_trip(12, "Red Sea 2025").model_dump(), "id": 12},
        ]
        monkeypatch.setattr(
            trips_module.crud_trips, "get_multi", AsyncMock(return_value={"data": rows, "total_count": 2})
        )
        monkeypatch.setattr(
            trips_module,
            "get_locations_for_trips",
            AsyncMock(return_value={11: [_read("Moalboal"), _read("Bohol")], 12: []}),
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

        assert [[location["name"] for location in trip["locations"]] for trip in page["data"]] == [
            ["Moalboal", "Bohol"],
            [],
        ]
        # The internal id the child rows hang off is not part of the public shape.
        assert "id" not in page["data"][0]

    @pytest.mark.asyncio
    async def test_a_search_goes_through_the_conditions_not_the_plain_listing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        get_multi = AsyncMock(return_value={"data": [], "total_count": 0})
        search_multi: Any = AsyncMock(return_value={"data": [], "total_count": 0})
        monkeypatch.setattr(trips_module.crud_trips, "get_multi", get_multi)
        monkeypatch.setattr(trips_module, "search_multi", search_multi)
        monkeypatch.setattr(trips_module, "get_locations_for_trips", AsyncMock(return_value={}))

        with patch.object(cache_module, "client", _FakeRedis()):
            await trips_module._cached_read_trips(
                _get_request(),
                user_id=USER_ID,
                user_uuid=USER_UUID,
                db=MagicMock(),
                page=1,
                items_per_page=10,
                search="moalboal",
            )

        get_multi.assert_not_awaited()
        # Compared as compiled SQL: two `ColumnElement`s that mean the same thing are not
        # `==` to each other, they *are* an equality expression.
        assert _as_sql(*search_multi.await_args.kwargs["conditions"]) == _as_sql(
            *trips_module._search_conditions(user_id=USER_ID, term="moalboal")
        )
        assert search_multi.await_args.kwargs["sort_column"] == "start_date"

    @pytest.mark.asyncio
    async def test_the_list_key_is_the_one_writes_sweep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(trips_module, "search_multi", AsyncMock(return_value={"data": [], "total_count": 0}))
        monkeypatch.setattr(trips_module, "get_locations_for_trips", AsyncMock(return_value={}))
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
    async def test_a_single_trip_embeds_its_locations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        trip = _internal_trip()
        monkeypatch.setattr(trips_module.crud_trips, "get", AsyncMock(return_value=trip))
        monkeypatch.setattr(
            trips_module, "get_locations_for_trip", AsyncMock(return_value=[_read("Moalboal"), _read("Bohol")])
        )
        redis = _FakeRedis()

        with patch.object(cache_module, "client", redis):
            # Annotated `Any` because what comes back is what the client will get: on a
            # GET the decorator returns the round-tripped JSON, not the `TripRead` the
            # signature promises.
            body: Any = await trips_module._cached_read_trip(
                _get_request(), uuid=trip.uuid, owner_uuid=USER_UUID, db=MagicMock()
            )

        assert [location["name"] for location in body["locations"]] == ["Moalboal", "Bohol"]
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
        sql = _as_sql(*trips_module._search_conditions(user_id=USER_ID, term="moalboal"))

        # OR'd across the two tables: a trip called "Moalboal 2026" and one merely *named
        # after Cebu* that went there both have to match.
        assert "trip.name ILIKE '%moalboal%'" in sql
        assert "EXISTS" in sql
        assert "trip_location.name ILIKE '%moalboal%'" in sql
        # The display name too, so "philippines" finds a trip whose places are all towns.
        assert "trip_location.display_name ILIKE '%moalboal%'" in sql

    def test_the_exists_is_correlated_to_the_trip_being_matched(self) -> None:
        """Without the correlation every trip matches as soon as *any* trip in the table
        has a location with the term in it."""
        sql = _as_sql(*trips_module._search_conditions(user_id=USER_ID, term="moalboal"))

        assert "trip_location.trip_id = trip.id" in sql

    def test_stays_inside_the_callers_own_trips(self) -> None:
        sql = _as_sql(*trips_module._search_conditions(user_id=7, term="moalboal"))

        assert "trip.user_id = 7" in sql
        # Ownership is the whole scope now: trips are hard-deleted, so there is no
        # liveness clause the EXISTS could be written outside of.
        assert "is_deleted" not in sql

    def test_a_wildcard_in_the_term_matches_literally(self) -> None:
        """`escape_like` on both sides of the OR, same as every other picker - otherwise
        a search for "50%" is a search for everything, in the child table too."""
        sql = _as_sql(*trips_module._search_conditions(user_id=USER_ID, term="50%"))

        # The rendered literal doubles each backslash; what matters is that the `%` the
        # diver typed arrives escaped rather than as a live wildcard, under an `ESCAPE`.
        assert sql.count(f"'%50{'\\' * 2}%%' ESCAPE") == 3


@pytest.fixture
def trip(db: Session, diver: User) -> Trip:
    return create_trip(db, diver)


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestReplaceLocations:
    """`replace_locations_for_trip` against a live Postgres - what the table holds after
    a write, which is the half a stubbed session cannot answer.

    A trip's locations are replaced wholesale rather than diffed, so every edit is a
    `DELETE` plus inserts numbered from the *new* list. Both halves fail quietly if they
    regress: a lost `DELETE` leaves the surplus rows of the previous, longer list behind,
    and positions carried over from it re-order the page the next time it is read. Either
    one looks correct until a trip is edited twice.
    """

    @staticmethod
    def _inputs(*names: str) -> list[TripLocationInput]:
        return [TripLocationInput(name=name) for name in names]

    @staticmethod
    def _rows(db: Session, trip: Trip) -> list[tuple[str, int]]:
        rows = db.query(TripLocation).filter(TripLocation.trip_id == trip.id).order_by(TripLocation.id).all()
        return [(row.name, row.position) for row in rows]

    @pytest.mark.asyncio
    async def test_position_is_the_index_in_the_list_that_was_sent(
        self, db: Session, async_db: AsyncSession, trip: Trip
    ) -> None:
        await replace_locations_for_trip(db=async_db, trip_id=trip.id, locations=self._inputs("Moalboal", "Bohol"))

        assert self._rows(db, trip) == [("Moalboal", 0), ("Bohol", 1)]

    @pytest.mark.asyncio
    async def test_a_shorter_list_leaves_none_of_the_longer_one_behind(
        self, db: Session, async_db: AsyncSession, trip: Trip
    ) -> None:
        """The surplus rows have to go, and the survivor has to be re-numbered from the
        new list - a "Bohol" still sitting at position 1 would read back as a second
        place the diver deleted."""
        await replace_locations_for_trip(
            db=async_db, trip_id=trip.id, locations=self._inputs("Moalboal", "Bohol", "Anilao")
        )

        await replace_locations_for_trip(db=async_db, trip_id=trip.id, locations=self._inputs("Bohol"))

        assert self._rows(db, trip) == [("Bohol", 0)]

    @pytest.mark.asyncio
    async def test_an_empty_list_clears_them(self, db: Session, async_db: AsyncSession, trip: Trip) -> None:
        await replace_locations_for_trip(db=async_db, trip_id=trip.id, locations=self._inputs("Moalboal"))

        await replace_locations_for_trip(db=async_db, trip_id=trip.id, locations=[])

        assert self._rows(db, trip) == []

    @pytest.mark.asyncio
    async def test_duplicate_names_are_legal(self, db: Session, async_db: AsyncSession, trip: Trip) -> None:
        """Two stays in the same town on one trip. There is no unique constraint, and
        `position` is the only thing telling the rows apart - which is why the read is
        ordered by it and nothing dedupes."""
        await replace_locations_for_trip(db=async_db, trip_id=trip.id, locations=self._inputs("Dahab", "Dahab"))

        assert self._rows(db, trip) == [("Dahab", 0), ("Dahab", 1)]

    @pytest.mark.asyncio
    async def test_another_trips_places_are_left_alone(
        self, db: Session, async_db: AsyncSession, diver: User, trip: Trip
    ) -> None:
        """The `DELETE` is scoped by `trip_id`, and a suite with one trip in it cannot
        notice when that stops being true."""
        neighbour = Trip(user_id=diver.id, name=f"Elsewhere {uuid7().hex[-8:]}", start_date=date(2026, 7, 1), notes="")
        db.add(neighbour)
        db.commit()
        await replace_locations_for_trip(db=async_db, trip_id=neighbour.id, locations=self._inputs("Koh Tao"))

        await replace_locations_for_trip(db=async_db, trip_id=trip.id, locations=self._inputs("Moalboal"))

        assert self._rows(db, neighbour) == [("Koh Tao", 0)]

    @pytest.mark.asyncio
    async def test_what_was_written_is_what_reads_back(self, db: Session, async_db: AsyncSession, trip: Trip) -> None:
        """Round-trips every column, including the antimeridian-legal `west > east`."""
        written = TripLocationInput(**{**MOALBOAL, "bbox_west": 179.9, "bbox_east": -179.9})

        await replace_locations_for_trip(db=async_db, trip_id=trip.id, locations=[written])

        (read_back,) = await get_locations_for_trip(db=async_db, trip_id=trip.id)
        assert read_back.model_dump() == written.model_dump()


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestSearchAgainstPostgres:
    """`_search_conditions` executed rather than compiled.

    The compiled-SQL assertions above cannot settle the one failure that matters here:
    an EXISTS that does not correlate to the row being matched renders the same
    `trip_location.trip_id = trip.id` text and matches *every* trip as soon as any trip
    in the table has a location with the term in it. Only running it against rows tells
    the two apart - and getting it wrong would show one diver a page of trips they have
    no business seeing named after a place they never went.
    """

    @staticmethod
    async def _matching_names(db: AsyncSession, user_id: int, term: str) -> list[str]:
        rows = await db.execute(select(Trip.name).where(*trips_module._search_conditions(user_id=user_id, term=term)))
        return sorted(name for (name,) in rows)

    @pytest_asyncio.fixture
    async def _seeded(self, db: Session, async_db: AsyncSession, diver: User) -> tuple[str, str]:
        """Two trips, one of which went to Moalboal - and neither of which is named it."""
        tag = uuid7().hex[-8:]
        went, stayed = (
            Trip(user_id=diver.id, name=f"Cebu {tag}", start_date=date(2026, 6, 1), notes=""),
            Trip(user_id=diver.id, name=f"Elsewhere {tag}", start_date=date(2026, 7, 1), notes=""),
        )
        db.add_all([went, stayed])
        db.commit()
        await replace_locations_for_trip(
            db=async_db,
            trip_id=went.id,
            locations=[TripLocationInput(name=f"Moalboal {tag}", display_name=f"Moalboal, Cebu, Philippines {tag}")],
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
    async def test_the_display_name_matches_too(
        self, async_db: AsyncSession, diver: User, _seeded: tuple[str, str]
    ) -> None:
        """ "philippines" has to find a trip whose places are all named after towns."""
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
