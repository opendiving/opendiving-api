"""A write of a parent and its children is one transaction: a constraint any of its rows breaks
is a 422 that leaves nothing of the request behind, rather than a bare parent row a retry
duplicates or a rename the caches go on serving."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from src.app.api.v1 import courses as courses_module
from src.app.api.v1 import dive_sites as dive_sites_module
from src.app.api.v1 import dives as dives_module
from src.app.api.v1 import gear_sets as gear_sets_module
from src.app.api.v1 import trips as trips_module
from src.app.core.exceptions.http_exceptions import UnprocessableEntityException
from src.app.models.course import Course
from src.app.models.dive import Dive
from src.app.models.dive_mixture import DiveMixture
from src.app.models.dive_site import DiveSite
from src.app.models.gear_set import GearSet
from src.app.models.tag import Tag
from src.app.models.trip import Trip
from src.app.models.trip_part import TripPart
from src.app.models.user import User
from src.app.schemas.course import CourseCreate, CourseUpdateRequest
from src.app.schemas.dive import DiveCreateRequest, DiveUpdateRequest
from src.app.schemas.dive_site import DiveSiteCreate, DiveSiteUpdateRequest
from src.app.schemas.gear_set import GearSetCreateRequest, GearSetUpdateRequest
from src.app.schemas.trip import TripCreate, TripUpdateRequest
from tests.conftest import db_available

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

# `ck_dive_mixture_oxygen_range` is the only bound on it: no Pydantic one stands in front.
OUT_OF_RANGE_OXYGEN = 150.0
# No row has this id, so a child naming it fails its foreign key on insert - the concurrent
# delete between resolve and insert that is the one way a real request gets there.
MISSING_ID = 2_000_000_000


@pytest.fixture(autouse=True)
def no_redis(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    stubs: dict[str, AsyncMock] = {}
    for module in (dives_module, trips_module, courses_module, dive_sites_module, gear_sets_module):
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
            trips_module, "_resolve_accommodations", AsyncMock(return_value={accommodation: MISSING_ID})
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
            trips_module, "_resolve_accommodations", AsyncMock(return_value={accommodation: MISSING_ID})
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


class TestACourse:
    @pytest.mark.asyncio
    async def test_a_create_whose_person_vanished_stores_no_course(
        self, monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession, diver: User, no_redis: dict[str, AsyncMock]
    ) -> None:
        monkeypatch.setattr(courses_module, "resolve_people_references", AsyncMock(return_value=[(MISSING_ID, None)]))

        with pytest.raises(UnprocessableEntityException, match="Person not found"):
            await courses_module.write_course(
                request=MagicMock(),
                course=CourseCreate.model_validate({"name": "Open Water", "agency": "padi"}),
                current_user=_as(diver),
                db=async_db,
            )

        assert await _count(async_db, Course, Course.user_id == diver.id) == 0
        no_redis["invalidate_course_caches"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_patch_whose_person_vanished_leaves_the_course_as_it_was(
        self, monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession, diver: User, no_redis: dict[str, AsyncMock]
    ) -> None:
        created = await courses_module.write_course(
            request=MagicMock(),
            course=CourseCreate.model_validate({"name": "Open Water", "agency": "padi"}),
            current_user=_as(diver),
            db=async_db,
        )
        no_redis["invalidate_course_caches"].reset_mock()
        monkeypatch.setattr(courses_module, "resolve_people_references", AsyncMock(return_value=[(MISSING_ID, None)]))

        with pytest.raises(UnprocessableEntityException, match="Person not found"):
            await courses_module.patch_course(
                request=MagicMock(),
                uuid=created.uuid,
                values=CourseUpdateRequest.model_validate(
                    {"name": "Advanced", "people": [{"person_uuid": str(uuid7())}]}
                ),
                current_user=_as(diver),
                db=async_db,
            )

        assert await async_db.scalar(select(Course.name).where(Course.uuid == created.uuid)) == "Open Water"
        no_redis["invalidate_course_caches"].assert_not_awaited()


class TestADiveSite:
    @pytest.mark.asyncio
    async def test_a_create_whose_tag_vanished_stores_no_site(
        self, monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession, diver: User, no_redis: dict[str, AsyncMock]
    ) -> None:
        monkeypatch.setattr(dive_sites_module, "_resolved_tag_ids", AsyncMock(return_value=[MISSING_ID]))

        with pytest.raises(UnprocessableEntityException, match="Tag not found"):
            await dive_sites_module.write_dive_site(
                request=MagicMock(),
                dive_site=DiveSiteCreate.model_validate({"name": "Blue Hole", "tags": ["Wall"]}),
                current_user=_as(diver),
                db=async_db,
            )

        assert await _count(async_db, DiveSite, DiveSite.user_id == diver.id) == 0
        no_redis["invalidate_dive_site_caches"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_patch_whose_tag_vanished_leaves_the_site_and_its_new_tags_as_they_were(
        self, monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession, diver: User, no_redis: dict[str, AsyncMock]
    ) -> None:
        created = await dive_sites_module.write_dive_site(
            request=MagicMock(),
            dive_site=DiveSiteCreate.model_validate({"name": "Blue Hole"}),
            current_user=_as(diver),
            db=async_db,
        )
        no_redis["invalidate_dive_site_caches"].reset_mock()
        resolve = dive_sites_module._resolved_tag_ids

        async def a_new_tag_and_a_vanished_one(*args: Any, **kwargs: Any) -> list[int]:
            return [*await resolve(*args, **kwargs), MISSING_ID]

        monkeypatch.setattr(dive_sites_module, "_resolved_tag_ids", a_new_tag_and_a_vanished_one)

        with pytest.raises(UnprocessableEntityException, match="Tag not found"):
            await dive_sites_module.patch_dive_site(
                request=MagicMock(),
                uuid=created.uuid,
                values=DiveSiteUpdateRequest.model_validate({"name": "Canyon", "tags": ["Wall"]}),
                current_user=_as(diver),
                db=async_db,
            )

        assert await async_db.scalar(select(DiveSite.name).where(DiveSite.uuid == created.uuid)) == "Blue Hole"
        assert await _count(async_db, Tag, Tag.user_id == diver.id) == 0
        no_redis["invalidate_dive_site_caches"].assert_not_awaited()


class TestAGearSet:
    @pytest.mark.asyncio
    async def test_a_create_whose_item_vanished_stores_no_set(
        self, monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession, diver: User, no_redis: dict[str, AsyncMock]
    ) -> None:
        monkeypatch.setattr(gear_sets_module, "_resolve_item_ids", AsyncMock(return_value=[MISSING_ID]))

        with pytest.raises(UnprocessableEntityException, match="Gear item not found"):
            await gear_sets_module.write_gear_set(
                request=MagicMock(),
                gear_set=GearSetCreateRequest.model_validate({"name": "Cold water", "gear_item_uuids": [str(uuid7())]}),
                current_user=_as(diver),
                db=async_db,
            )

        assert await _count(async_db, GearSet, GearSet.user_id == diver.id) == 0
        no_redis["invalidate_gear_caches"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_patch_whose_item_vanished_leaves_the_set_as_it_was(
        self, monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession, diver: User, no_redis: dict[str, AsyncMock]
    ) -> None:
        created = await gear_sets_module.write_gear_set(
            request=MagicMock(),
            gear_set=GearSetCreateRequest.model_validate({"name": "Cold water"}),
            current_user=_as(diver),
            db=async_db,
        )
        no_redis["invalidate_gear_caches"].reset_mock()
        monkeypatch.setattr(gear_sets_module, "_resolve_item_ids", AsyncMock(return_value=[MISSING_ID]))

        with pytest.raises(UnprocessableEntityException, match="Gear item not found"):
            await gear_sets_module.patch_gear_set(
                request=MagicMock(),
                uuid=created.uuid,
                values=GearSetUpdateRequest.model_validate({"name": "Warm water", "gear_item_uuids": [str(uuid7())]}),
                current_user=_as(diver),
                db=async_db,
            )

        assert await async_db.scalar(select(GearSet.name).where(GearSet.uuid == created.uuid)) == "Cold water"
        no_redis["invalidate_gear_caches"].assert_not_awaited()
