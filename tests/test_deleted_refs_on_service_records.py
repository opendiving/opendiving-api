"""Tests that a service record stops naming a schedule the diver has deleted — and that
it goes on naming a deleted gear *item*, which is the deliberate half.

The fifth and last surface in the family `test_deleted_refs_on_dive_reads.py` opened, and
the only one where the answer was to filter one resolver and leave its neighbour alone.
The two look identical from the outside and are not: `gear_service_schedule_uuid` is
already `UUID | None` and every call site `.get()`s it, so hiding a deleted schedule lands
in a state the contract already describes, while `gear_item_uuid` is required and indexed
directly, so the same filter there is a `KeyError` rather than a null. See "The
service-record resolvers split, and only one of them was the same question" in
DECISIONS.md.

Both halves are pinned here, and the second matters more than it looks: a test asserting
that a deleted item's uuid *still comes through* is what stands between the codebase and
someone tidying up the asymmetry into a 500.

These run against a live Postgres and skip themselves otherwise - see CONTRIBUTING.md for
why a run on the host needs `POSTGRES_SERVER=localhost` to make them execute.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.api.v1.gear_service import _schedule_uuids_by_id
from src.app.crud.crud_gear_items import get_gear_item_uuids_by_id
from src.app.models.user import User
from src.app.services.gear_service import soft_delete_schedules_for_gear_item
from tests.conftest import db_available
from tests.helpers.generators import (
    create_gear_item,
    create_gear_service_record,
    create_gear_service_schedule,
)


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestDeletedSchedulesAreNotNamedOnRecords:
    @pytest.mark.asyncio
    async def test_a_deleted_schedule_stops_resolving(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """The bug in one assertion: `GET /gear-service-schedule/{uuid}` 404s for this
        schedule and `?gear_service_schedule_uuid=` 422s on it, so a record must not go on
        handing the uuid out."""
        item = create_gear_item(db, diver)
        live = create_gear_service_schedule(db, diver, item)
        deleted = create_gear_service_schedule(db, diver, item, is_deleted=True)

        by_id = await _schedule_uuids_by_id(async_db, schedule_ids=[live.id, deleted.id])

        assert by_id == {live.id: live.uuid}

    @pytest.mark.asyncio
    async def test_the_record_reads_back_with_a_null_schedule(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """The shape that made filtering cheap here: the miss becomes the `null` the field
        was already documented to carry, via the `.get()` at both call sites, rather than
        needing a flag or a tombstone."""
        item = create_gear_item(db, diver)
        deleted = create_gear_service_schedule(db, diver, item, is_deleted=True)
        record = create_gear_service_record(db, diver, item, schedule=deleted)

        by_id = await _schedule_uuids_by_id(async_db, schedule_ids=[record.gear_service_schedule_id])

        assert by_id.get(record.gear_service_schedule_id) is None

    @pytest.mark.asyncio
    async def test_a_record_that_never_had_a_schedule_is_unaffected(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """`gear_service_schedule_id` is nullable, so "never attached" and "attached to
        something since deleted" arrive here as the same `None` and must stay
        indistinguishable - that equivalence is what the field's existing contract rests
        on."""
        item = create_gear_item(db, diver)
        record = create_gear_service_record(db, diver, item)

        assert record.gear_service_schedule_id is None
        assert await _schedule_uuids_by_id(async_db, schedule_ids=[record.gear_service_schedule_id]) == {}

    @pytest.mark.asyncio
    async def test_deleting_the_item_takes_its_schedules_off_its_records(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """The path that makes this reachable at all: `erase_gear_item` soft-deletes the
        item's schedules (`soft_delete_schedules_for_gear_item`) while deliberately keeping
        its records, so deleting an item is what strands a record's schedule reference."""
        item = create_gear_item(db, diver)
        schedule = create_gear_service_schedule(db, diver, item)
        record = create_gear_service_record(db, diver, item, schedule=schedule)

        await soft_delete_schedules_for_gear_item(db=async_db, gear_item_id=item.id)

        by_id = await _schedule_uuids_by_id(async_db, schedule_ids=[record.gear_service_schedule_id])

        assert by_id == {}


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestDeletedItemsAreStillNamedOnRecords:
    """The deliberate non-fix, pinned so it cannot be tidied away into a 500.

    `gear_item_uuid` is required on `GearServiceRecordRead`/`GearServiceScheduleRead` and
    every caller indexes `get_gear_item_uuids_by_id`'s mapping directly, so filtering it
    would raise `KeyError` on every record of a deleted item rather than degrading to a
    null the way the schedule half does.
    """

    @pytest.mark.asyncio
    async def test_a_deleted_items_uuid_still_resolves(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        deleted = create_gear_item(db, diver, is_deleted=True)

        by_id = await get_gear_item_uuids_by_id(async_db, gear_item_ids=[deleted.id])

        assert by_id == {deleted.id: deleted.uuid}

    @pytest.mark.asyncio
    async def test_every_id_a_caller_holds_comes_back(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """The property the unguarded indexing at the call sites depends on: every id in,
        every id out. A filter added here breaks this, and the break surfaces as a 500 in
        a different file."""
        live, deleted = create_gear_item(db, diver), create_gear_item(db, diver, is_deleted=True)
        create_gear_service_record(db, diver, deleted)

        by_id = await get_gear_item_uuids_by_id(async_db, gear_item_ids=[live.id, deleted.id])

        assert set(by_id) == {live.id, deleted.id}
