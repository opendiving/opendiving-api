"""Tests for `GET /user/trip-places` (`api/v1/users.py`) and the query behind it
(`crud/crud_trip_parts.py::get_places_for_user`).

The query runs against a live Postgres: what it has to get right - the `DISTINCT ON`, the
join to the owner, the rows it leaves out - is what the database does with rows. It skips
itself when no database is reachable; see CONTRIBUTING.md for why a run on the host needs
`POSTGRES_SERVER=localhost`.
"""

from datetime import date
from fnmatch import fnmatch
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1 import users as users_module
from src.app.core.utils import cache as cache_module
from src.app.core.utils.cache import across_builds, namespaced
from src.app.core.utils.owned_resource_cache import OwnedResourceCache
from src.app.crud.crud_trip_parts import get_places_for_user
from src.app.models.trip import Trip
from src.app.models.trip_part import TripPart
from src.app.models.user import User
from src.app.schemas.location import LocationRead
from tests.conftest import db_available

DAHAB = {
    "name": "Dahab, South Sinai, Egypt",
    "latitude": 28.49,
    "longitude": 34.51,
    "bbox_south": 28.45,
    "bbox_north": 28.53,
    "bbox_west": 34.47,
    "bbox_east": 34.55,
}
MOALBOAL = {"name": "Moalboal, Philippines", "latitude": 9.94, "longitude": 123.39}


def _trip(db: Session, user: User, *parts: dict[str, Any]) -> Trip:
    trip = Trip(user_id=user.id, name=f"Trip {uuid7().hex[-8:]}", notes="")
    db.add(trip)
    db.commit()
    for position, part in enumerate(parts):
        db.add(TripPart(trip_id=trip.id, position=position, **part))
    db.commit()
    return trip


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestGetPlacesForUser:
    @pytest.mark.asyncio
    async def test_a_diver_with_no_trips_has_no_places(self, async_db: AsyncSession, diver: User) -> None:
        assert await get_places_for_user(async_db, diver.id) == []

    @pytest.mark.asyncio
    async def test_lists_every_place_across_every_trip(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        _trip(db, diver, DAHAB)
        _trip(db, diver, MOALBOAL)

        places = await get_places_for_user(async_db, diver.id)

        assert places == [LocationRead(**DAHAB), LocationRead(**MOALBOAL)]

    @pytest.mark.asyncio
    async def test_leaves_out_parts_a_map_cannot_draw(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """A dated part with no place, and a typed name the geocoder never placed."""
        _trip(db, diver, {"start_date": date(2026, 3, 1)}, {"name": "Somewhere in the Red Sea"})

        assert await get_places_for_user(async_db, diver.id) == []

    @pytest.mark.asyncio
    async def test_the_same_place_on_two_trips_is_one(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """Even when the two picks carried different boxes - the later write is kept."""
        _trip(db, diver, DAHAB, DAHAB)
        _trip(db, diver, {**DAHAB, "bbox_south": 28.40})

        places = await get_places_for_user(async_db, diver.id)

        assert places == [LocationRead(**{**DAHAB, "bbox_south": 28.40})]

    @pytest.mark.asyncio
    async def test_the_same_name_somewhere_else_is_another_place(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        _trip(db, diver, MOALBOAL, {**MOALBOAL, "latitude": 9.95})

        assert len(await get_places_for_user(async_db, diver.id)) == 2

    @pytest.mark.asyncio
    async def test_another_divers_trips_are_not_listed(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        _trip(db, other_diver, DAHAB)

        assert await get_places_for_user(async_db, diver.id) == []


class TestTheCache:
    @pytest.mark.asyncio
    async def test_is_swept_with_the_trip_list(self) -> None:
        """Under the list's prefix, so `invalidate_trip_caches` drops it after every trip
        create, update and delete without naming it."""
        redis = MagicMock(get=AsyncMock(return_value=None), set=AsyncMock(), expire=AsyncMock())
        request = MagicMock()
        request.method = "GET"

        with (
            patch.object(cache_module, "client", redis),
            patch.object(users_module, "get_places_for_user", AsyncMock(return_value=[LocationRead(**DAHAB)])),
        ):
            body: Any = await users_module._cached_trip_places(request, user_id=7, db=MagicMock())

        assert body == [LocationRead(**DAHAB).model_dump()]
        ((key, _),) = [call.args for call in redis.set.await_args_list]
        assert key == namespaced("user_7_trips:places:7")
        assert fnmatch(key, across_builds(OwnedResourceCache.list_cache_pattern("trips", 7)))
