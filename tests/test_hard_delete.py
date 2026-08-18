"""The delete itself, against a live Postgres: the row is gone, and so is what pointed
at it.

`test_export_loader.py` pins the *consequences* of the cascades, because export is where
a dangling reference used to hurt. This pins the two facts underneath them, neither of
which any other test states.

The first is that `crud_X.delete` really issues a `DELETE`. FastCRUD branches on whether
the model carries `is_deleted` and silently flags the row instead when it does, so
re-adding `SoftDeleteMixin` to any of these five - or to a sixth model added later, by
copying one of them - would turn every cascade in this change back off with no test
failing anywhere. These call the CRUD layer rather than the route deliberately: the route
tests all stub `delete`, which is exactly the layer in question.

The second is that a name frees its slot. The five `ux_*` indexes were partial on
`is_deleted` so a diver could reuse a deleted site's name; hard delete gives that for
free, and the app-level `*_name_exists` checks in front of them have to agree.

Skipped when no database is reachable. On a developer's machine that means
`POSTGRES_SERVER=localhost` (`src/.env` points at the compose hostname, which does not
resolve on the host); CI sets it and fails the job if anything skips. See CONTRIBUTING.md.
"""

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.api.dependencies import fetch_owned_or_raise
from src.app.core.exceptions.http_exceptions import NotFoundException
from src.app.crud.crud_dive_sites import crud_dive_sites, dive_site_name_exists
from src.app.crud.crud_gear_items import crud_gear_items, gear_item_name_exists
from src.app.crud.crud_gear_service_schedules import (
    crud_gear_service_schedules,
    resolve_schedule_for_user,
    schedule_kind_exists,
)
from src.app.crud.crud_gear_sets import crud_gear_sets, gear_set_name_exists
from src.app.crud.crud_trips import crud_trips, trip_name_exists
from src.app.models.dive_site import DiveSite
from src.app.models.gear_item import GearItem
from src.app.models.gear_service_schedule import GearServiceSchedule
from src.app.models.gear_set import GearSet
from src.app.models.trip import Trip
from src.app.models.user import User
from src.app.schemas.dive_site import DiveSiteReadInternal
from src.app.schemas.gear_item import GearItemReadInternal
from src.app.schemas.gear_set import GearSetReadInternal
from src.app.schemas.trip import TripReadInternal
from tests.conftest import db_available
from tests.helpers.generators import (
    create_dive_site,
    create_gear_item,
    create_gear_service_schedule,
    create_gear_set,
    create_trip,
    create_user,
)

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")


@pytest.fixture
def diver(db: Session) -> User:
    return create_user(db)


async def _count(async_db: AsyncSession, model: Any, row_id: int) -> int:
    result = await async_db.execute(select(func.count()).select_from(model).where(model.id == row_id))
    return int(result.scalar_one())


class TestTheRowIsActuallyRemoved:
    """One case per resource, because each has its own `FastCRUD` instance and its own
    chance to be wired back to a soft-deleting model."""

    @pytest.mark.asyncio
    async def test_deleting_a_trip(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        trip = create_trip(db, diver)

        await crud_trips.delete(db=async_db, uuid=trip.uuid)

        assert await _count(async_db, Trip, trip.id) == 0

    @pytest.mark.asyncio
    async def test_deleting_a_dive_site(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        site = create_dive_site(db, diver)

        await crud_dive_sites.delete(db=async_db, uuid=site.uuid)

        assert await _count(async_db, DiveSite, site.id) == 0

    @pytest.mark.asyncio
    async def test_deleting_a_gear_item(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        item = create_gear_item(db, diver)

        await crud_gear_items.delete(db=async_db, uuid=item.uuid)

        assert await _count(async_db, GearItem, item.id) == 0

    @pytest.mark.asyncio
    async def test_deleting_a_gear_set(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        gear_set = create_gear_set(db, diver)

        await crud_gear_sets.delete(db=async_db, uuid=gear_set.uuid)

        assert await _count(async_db, GearSet, gear_set.id) == 0

    @pytest.mark.asyncio
    async def test_deleting_a_service_schedule(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        item = create_gear_item(db, diver)
        schedule = create_gear_service_schedule(db, diver, item)

        await crud_gear_service_schedules.delete(db=async_db, uuid=schedule.uuid)

        assert await _count(async_db, GearServiceSchedule, schedule.id) == 0


class TestASecondDeleteIsA404:
    """`DELETE` stopped being idempotent, and that is a deliberate contract change.

    The idempotency insured against a half-failed multi-statement delete; a single
    statement in one transaction cannot half-fail. Every one of these routes resolves the
    row before doing anything else, so what a second call actually meets is the lookup
    below returning nothing - which is what makes "moves nothing on a retry" true rather
    than just untested.
    """

    async def _owned(self, async_db: AsyncSession, crud: Any, uuid: Any, diver: User, schema: type) -> Any:
        return await fetch_owned_or_raise(
            db=async_db,
            crud=crud,
            uuid=uuid,
            current_user={"id": diver.id, "uuid": diver.uuid},
            schema=schema,
            not_found_message="Not found",
        )

    @pytest.mark.asyncio
    async def test_a_deleted_trip_no_longer_resolves(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        trip = create_trip(db, diver)
        await crud_trips.delete(db=async_db, uuid=trip.uuid)

        with pytest.raises(NotFoundException):
            await self._owned(async_db, crud_trips, trip.uuid, diver, TripReadInternal)

    @pytest.mark.asyncio
    async def test_a_deleted_site_no_longer_resolves(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        site = create_dive_site(db, diver)
        await crud_dive_sites.delete(db=async_db, uuid=site.uuid)

        with pytest.raises(NotFoundException):
            await self._owned(async_db, crud_dive_sites, site.uuid, diver, DiveSiteReadInternal)

    @pytest.mark.asyncio
    async def test_a_deleted_gear_item_no_longer_resolves(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        item = create_gear_item(db, diver)
        await crud_gear_items.delete(db=async_db, uuid=item.uuid)

        with pytest.raises(NotFoundException):
            await self._owned(async_db, crud_gear_items, item.uuid, diver, GearItemReadInternal)

    @pytest.mark.asyncio
    async def test_a_deleted_gear_set_no_longer_resolves(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        gear_set = create_gear_set(db, diver)
        await crud_gear_sets.delete(db=async_db, uuid=gear_set.uuid)

        with pytest.raises(NotFoundException):
            await self._owned(async_db, crud_gear_sets, gear_set.uuid, diver, GearSetReadInternal)

    @pytest.mark.asyncio
    async def test_a_deleted_schedule_no_longer_resolves(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """The schedule route resolves through `resolve_schedule_for_user` rather than
        `fetch_owned_or_raise`, so it needs its own case."""
        item = create_gear_item(db, diver)
        schedule = create_gear_service_schedule(db, diver, item)
        await crud_gear_service_schedules.delete(db=async_db, uuid=schedule.uuid)

        assert await resolve_schedule_for_user(db=async_db, schedule_uuid=schedule.uuid, user_id=diver.id) is None


class TestADeletedNameFreesItsSlot:
    """What the partial `ux_*` predicates used to buy, now had for free.

    Each of these `*_name_exists` helpers is the friendly-422 half of a real unique index,
    and each used to carry an `is_deleted IS false` matching the index's `postgresql_where`.
    Both halves lost it together; a helper that kept one would refuse a name the index
    would happily accept, which reads to the diver as "that name is taken" for a row that
    does not exist.
    """

    @pytest.mark.asyncio
    async def test_a_trip_name(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        trip = create_trip(db, diver)
        assert await trip_name_exists(async_db, user_id=diver.id, name=trip.name) is True

        await crud_trips.delete(db=async_db, uuid=trip.uuid)

        assert await trip_name_exists(async_db, user_id=diver.id, name=trip.name) is False

    @pytest.mark.asyncio
    async def test_a_dive_site_name_and_location(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        site = create_dive_site(db, diver)
        assert await dive_site_name_exists(async_db, user_id=diver.id, name=site.name, location=site.location) is True

        await crud_dive_sites.delete(db=async_db, uuid=site.uuid)

        assert await dive_site_name_exists(async_db, user_id=diver.id, name=site.name, location=site.location) is False

    @pytest.mark.asyncio
    async def test_a_gear_item_brand_and_name(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        item = create_gear_item(db, diver)
        assert await gear_item_name_exists(async_db, user_id=diver.id, name=item.name, brand=item.brand) is True

        await crud_gear_items.delete(db=async_db, uuid=item.uuid)

        assert await gear_item_name_exists(async_db, user_id=diver.id, name=item.name, brand=item.brand) is False

    @pytest.mark.asyncio
    async def test_a_gear_set_name(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        gear_set = create_gear_set(db, diver)
        assert await gear_set_name_exists(async_db, user_id=diver.id, name=gear_set.name) is True

        await crud_gear_sets.delete(db=async_db, uuid=gear_set.uuid)

        assert await gear_set_name_exists(async_db, user_id=diver.id, name=gear_set.name) is False

    @pytest.mark.asyncio
    async def test_a_schedules_kind_and_label(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        item = create_gear_item(db, diver)
        schedule = create_gear_service_schedule(db, diver, item)
        assert await schedule_kind_exists(async_db, gear_item_id=item.id, kind=schedule.kind) is True

        await crud_gear_service_schedules.delete(db=async_db, uuid=schedule.uuid)

        assert await schedule_kind_exists(async_db, gear_item_id=item.id, kind=schedule.kind) is False
