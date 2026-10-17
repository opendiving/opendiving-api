"""A dive or trip write is one transaction: a constraint any of its rows breaks is a 422 that
leaves nothing of the request behind, rather than a bare parent row a retry duplicates."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from src.app.api.v1 import dives as dives_module
from src.app.api.v1 import trips as trips_module
from src.app.core.exceptions.http_exceptions import UnprocessableEntityException
from src.app.models.dive import Dive
from src.app.models.dive_mixture import DiveMixture
from src.app.models.tag import Tag
from src.app.models.trip import Trip
from src.app.models.trip_part import TripPart
from src.app.models.user import User
from src.app.schemas.dive import DiveCreateRequest, DiveUpdateRequest
from src.app.schemas.trip import TripCreate, TripUpdateRequest
from tests.conftest import db_available

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

# `ck_dive_mixture_oxygen_range` is the only bound on it: no Pydantic one stands in front.
OUT_OF_RANGE_OXYGEN = 150.0
# No contact has this id, so a part naming it fails `trip_part`'s foreign key on insert - the
# concurrent delete between resolve and insert that is the one way a real request gets there.
MISSING_CONTACT_ID = 2_000_000_000


@pytest.fixture(autouse=True)
def no_redis(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    stubs: dict[str, AsyncMock] = {}
    for module in (dives_module, trips_module):
        for name in dir(module):
            if name.startswith("invalidate_") and name.endswith("_caches"):
                stubs[name] = AsyncMock()
                monkeypatch.setattr(module, name, stubs[name])
    return stubs


def _as(user: User) -> dict[str, Any]:
    return {"id": user.id, "uuid": user.uuid, "username": user.username, "is_superuser": False}


async def _write_dive(async_db: AsyncSession, user: User, **members: Any) -> Any:
    body = {"dive_number": 1, "start_time": "2026-06-01T09:00:00+02:00", "submerged_time": 2700, **members}
    return await dives_module.write_dive(
        request=MagicMock(), dive=DiveCreateRequest.model_validate(body), current_user=_as(user), db=async_db
    )


async def _count(async_db: AsyncSession, model: Any, *where: Any) -> int:
    return int(await async_db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


class TestADive:
    @pytest.mark.asyncio
    async def test_a_create_whose_cylinder_breaks_a_check_stores_nothing(
        self, async_db: AsyncSession, diver: User, no_redis: dict[str, AsyncMock]
    ) -> None:
        with pytest.raises(UnprocessableEntityException, match="Oxygen percentage must be between 0 and 100"):
            await _write_dive(async_db, diver, tags=["Night"], mixtures=[{"oxygen": OUT_OF_RANGE_OXYGEN}])

        assert await _count(async_db, Dive, Dive.user_id == diver.id) == 0
        # Created by the same request, so it goes with the dive.
        assert await _count(async_db, Tag, Tag.user_id == diver.id) == 0
        no_redis["invalidate_dive_caches"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_patch_whose_cylinder_breaks_a_check_leaves_the_dive_as_it_was(
        self, async_db: AsyncSession, diver: User
    ) -> None:
        created = await _write_dive(async_db, diver, notes="before", mixtures=[{"oxygen": 32.0}])

        with pytest.raises(UnprocessableEntityException, match="Oxygen percentage must be between 0 and 100"):
            await dives_module.patch_dive(
                request=MagicMock(),
                uuid=created.uuid,
                values=DiveUpdateRequest.model_validate(
                    {"notes": "after", "tags": ["Night"], "mixtures": [{"oxygen": OUT_OF_RANGE_OXYGEN}]}
                ),
                current_user=_as(diver),
                db=async_db,
            )

        # The column update ran before the cylinders did, and is rolled back with them.
        assert await async_db.scalar(select(Dive.notes).where(Dive.uuid == created.uuid)) == "before"
        oxygen = await async_db.scalars(
            select(DiveMixture.oxygen).join(Dive, Dive.id == DiveMixture.dive_id).where(Dive.uuid == created.uuid)
        )
        assert list(oxygen) == [32.0]
        assert await _count(async_db, Tag, Tag.user_id == diver.id) == 0


class TestATrip:
    @pytest.mark.asyncio
    async def test_a_create_whose_part_fails_to_insert_stores_no_trip(
        self, monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession, diver: User
    ) -> None:
        accommodation = uuid7()
        monkeypatch.setattr(
            trips_module, "_resolve_accommodations", AsyncMock(return_value={accommodation: MISSING_CONTACT_ID})
        )

        with pytest.raises(UnprocessableEntityException, match="Trip parts could not be saved"):
            await trips_module.write_trip(
                request=MagicMock(),
                trip=TripCreate.model_validate(
                    {"name": "Cebu 2026", "parts": [{"accommodation_uuid": str(accommodation)}]}
                ),
                current_user=_as(diver),
                db=async_db,
            )

        assert await _count(async_db, Trip, Trip.user_id == diver.id) == 0

    @pytest.mark.asyncio
    async def test_a_patch_whose_part_fails_to_insert_leaves_the_trip_as_it_was(
        self, monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession, diver: User
    ) -> None:
        created = await trips_module.write_trip(
            request=MagicMock(),
            trip=TripCreate.model_validate({"name": "Cebu 2026", "parts": [{"location": {"name": "Moalboal"}}]}),
            current_user=_as(diver),
            db=async_db,
        )
        accommodation = uuid7()
        monkeypatch.setattr(
            trips_module, "_resolve_accommodations", AsyncMock(return_value={accommodation: MISSING_CONTACT_ID})
        )

        with pytest.raises(UnprocessableEntityException, match="Trip parts could not be saved"):
            await trips_module.patch_trip(
                request=MagicMock(),
                uuid=created.uuid,
                values=TripUpdateRequest.model_validate(
                    {"name": "Cebu 2027", "parts": [{"accommodation_uuid": str(accommodation)}]}
                ),
                current_user=_as(diver),
                db=async_db,
            )

        assert await async_db.scalar(select(Trip.name).where(Trip.uuid == created.uuid)) == "Cebu 2026"
        names = await async_db.scalars(
            select(TripPart.name).join(Trip, Trip.id == TripPart.trip_id).where(Trip.uuid == created.uuid)
        )
        assert list(names) == ["Moalboal"]
