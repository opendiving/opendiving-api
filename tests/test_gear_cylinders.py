"""A dive's cylinder linked to the gear item it is, the vessel's facts on that item, the item's
dive time, and a service rule running on hours of diving - through the real route bodies,
the import and the export.

The Postgres-backed classes skip themselves when no database is reachable - see
`test_dive_check_constraints.py`.
"""

import json
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import divejson
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api import router
from src.app.api.dependencies import get_current_user
from src.app.api.v1 import dives as dives_module
from src.app.api.v1 import gear_items as gear_items_module
from src.app.api.v1 import gear_service as gear_service_module
from src.app.core.config import settings
from src.app.core.exceptions.http_exceptions import UnprocessableEntityException
from src.app.core.setup import create_application
from src.app.crud.crud_dives import with_gear_item
from src.app.models.dive import Dive
from src.app.models.dive_mixture import DiveMixture
from src.app.models.gear_item import GearItem
from src.app.models.gear_service_record import GearServiceRecord
from src.app.models.gear_service_schedule import GearServiceSchedule
from src.app.models.user import User
from src.app.schemas.dive import DiveCreateRequest, DiveUpdateRequest
from src.app.schemas.dive_mixture import DiveMixtureRead
from src.app.schemas.gear_item import GearItemCreate, GearItemInfo, GearItemUpdate
from src.app.schemas.gear_service import (
    NEEDS_AN_INTERVAL,
    GearServiceRecordCreate,
    GearServiceScheduleCreate,
    GearServiceScheduleUpdate,
)
from src.app.schemas.logbook_import import ImportNoteCode
from src.app.schemas.parsed_dive import DiveMixtureSchema
from src.app.services.dive_files import LinkedCylinder, relabel_gas_numbers
from tests.conftest import db_available
from tests.helpers.generators import create_dive, create_gear_item, create_user
from tests.test_logbook_import import _apply, _export


def _as(user: User) -> dict[str, Any]:
    return {"id": user.id, "uuid": user.uuid, "username": user.username, "is_superuser": False}


def _vessel(db: Session, user: User, *, gear_type: str | None = "cylinder", volume: float | None = 11.1) -> GearItem:
    item = create_gear_item(db, user)
    item.type, item.volume, item.working_pressure, item.material = gear_type, volume, 207.0, "aluminium"
    db.commit()
    return item


@pytest.fixture(autouse=True)
def no_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """The routes end by dropping caches, and there is no Redis here to drop them in."""
    for module in (dives_module, gear_items_module, gear_service_module):
        for name in dir(module):
            if name.startswith("invalidate_") and name.endswith("_caches"):
                monkeypatch.setattr(module, name, AsyncMock())


def _dive_body(**members: Any) -> dict[str, Any]:
    return {"dive_number": 1, "start_time": "2026-06-01T09:00:00+02:00", "submerged_time": 2700, **members}


async def _write(async_db: AsyncSession, user: User, **members: Any) -> Any:
    return await dives_module.write_dive(
        request=MagicMock(),
        dive=DiveCreateRequest.model_validate(_dive_body(**members)),
        current_user=_as(user),
        db=async_db,
    )


async def _patch(async_db: AsyncSession, user: User, dive_uuid: Any, **members: Any) -> None:
    await dives_module.patch_dive(
        request=MagicMock(),
        uuid=dive_uuid,
        values=DiveUpdateRequest.model_validate(members),
        current_user=_as(user),
        db=async_db,
    )


async def _links(async_db: AsyncSession, dive_id: int) -> list[int | None]:
    rows = await async_db.scalars(
        select(DiveMixture.gear_item_id).where(DiveMixture.dive_id == dive_id).order_by(DiveMixture.id)
    )
    return list(rows.all())


class TestTheRequestShape:
    def test_a_vessel_linked_from_two_cylinders_is_refused_naming_it(self) -> None:
        vessel = uuid7()
        with pytest.raises(ValidationError, match=f"Gear item {vessel} is linked from two cylinders"):
            DiveCreateRequest.model_validate(
                _dive_body(mixtures=[{"gear_item_uuid": str(vessel)}, {"gear_item_uuid": str(vessel)}])
            )
        with pytest.raises(ValidationError, match="linked from two cylinders"):
            DiveUpdateRequest.model_validate({"mixtures": [{"gear_item_uuid": str(vessel)}] * 2})

    def test_the_repeat_is_a_422_on_the_wire(self) -> None:
        """The validator runs before the route body, so no database is reached - which is
        why this app skips the migrations the shared client's startup would run."""
        vessel = str(uuid7())
        repeat_app = create_application(router=router, settings=settings, apply_migrations_on_start=False)
        repeat_app.dependency_overrides[get_current_user] = lambda: {"id": 7, "uuid": uuid7()}
        with TestClient(repeat_app) as client:
            response = client.post(
                "/api/v1/dive", json=_dive_body(mixtures=[{"gear_item_uuid": vessel}, {"gear_item_uuid": vessel}])
            )

        assert response.status_code == 422
        assert "linked from two cylinders" in json.dumps(response.json())

    def test_the_vessel_s_facts_are_bounded_on_the_writes(self) -> None:
        with pytest.raises(ValidationError):
            GearItemCreate(name="Twin 12", volume=0)
        with pytest.raises(ValidationError):
            GearItemCreate(name="Twin 12", working_pressure=351)
        with pytest.raises(ValidationError):
            GearItemUpdate.model_validate({"material": "titanium"})
        assert GearItemCreate(name="Twin 12", volume=24, working_pressure=232, material="steel").volume == 24

    def test_an_hours_interval_alone_is_a_schedule_and_its_ceiling_holds(self) -> None:
        body = {"gear_item_uuid": str(uuid7()), "kind": "other", "starts_on": "2026-01-01"}
        assert GearServiceScheduleCreate.model_validate({**body, "interval_hours": 50}).interval_hours == 50
        with pytest.raises(ValidationError, match="in hours of diving"):
            GearServiceScheduleCreate.model_validate(body)
        with pytest.raises(ValidationError):
            GearServiceScheduleCreate.model_validate({**body, "interval_hours": 1_000_001})
        with pytest.raises(ValidationError):
            GearServiceScheduleUpdate.model_validate({"interval_hours": 1_000_001})


class TestRelabelCarriesTheLink:
    """The merge's and the attach path's rebuild, which a vessel link has to survive."""

    @staticmethod
    def _row(row_id: int, *, oxygen: float = 32.0, vessel: Any = None) -> DiveMixtureRead:
        gear_item = None if vessel is None else GearItemInfo(uuid=vessel, name="Steel 12")
        return DiveMixtureRead(id=row_id, oxygen=oxygen, helium=0.0, volume=12.0, gas_number=0, gear_item=gear_item)

    def test_a_pair_fills_a_blank_link_and_never_replaces_one(self) -> None:
        picked, standing = uuid7(), uuid7()

        _, filled = relabel_gas_numbers([self._row(9, vessel=picked)], [self._row(1)], fill=True)
        _, kept = relabel_gas_numbers([self._row(9, vessel=picked)], [self._row(1, vessel=standing)], fill=True)

        assert filled is not None and [row.gear_item_uuid for row in filled] == [picked]
        assert kept is None

    def test_a_link_fills_only_at_an_arrival(self) -> None:
        _, cylinders = relabel_gas_numbers([self._row(9, vessel=uuid7())], [self._row(1)], fill=False)
        assert cylinders is None

    def test_an_appended_cylinder_keeps_its_link_unless_the_dive_already_names_the_vessel(self) -> None:
        shared, own = uuid7(), uuid7()
        stored = [self._row(1, vessel=shared)]
        absorbed = [
            self._row(8, vessel=shared),
            self._row(9, oxygen=50.0, vessel=shared),
            self._row(10, oxygen=80.0, vessel=own),
        ]

        _, cylinders = relabel_gas_numbers(absorbed, stored, fill=True)

        assert cylinders is not None
        assert [row.gear_item_uuid for row in cylinders] == [shared, None, own]
        assert [row.oxygen for row in cylinders] == [32.0, 50.0, 80.0]

    def test_a_document_s_cylinder_carries_its_link_into_the_pairing(self) -> None:
        vessel = uuid7()
        incoming = [
            LinkedCylinder(
                oxygen=32.0,
                helium=0.0,
                gas_number=0,
                volume=None,
                start_pressure=None,
                end_pressure=None,
                po2_limit=None,
                role=None,
                gear_item_uuid=vessel,
            )
        ]

        _, cylinders = relabel_gas_numbers(incoming, [self._row(1)], fill=True)

        assert cylinders is not None and cylinders[0].gear_item_uuid == vessel

    def test_a_file_s_cylinder_links_nothing(self) -> None:
        _, cylinders = relabel_gas_numbers(
            [
                DiveMixtureSchema(
                    oxygen=32.0,
                    helium=0.0,
                    gas_number=0,
                    volume=None,
                    start_pressure=None,
                    end_pressure=None,
                    po2_limit=None,
                    role=None,
                )
            ],
            [self._row(1)],
            fill=True,
        )
        assert cylinders is None


class TestARecordingAttach:
    @pytest.mark.asyncio
    async def test_recounts_the_owner_s_gear_and_drops_its_caches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The re-derivation rewrites the dive's cylinders, and the vessels they link are what
        a gear item's figures count."""
        owner = {"id": 7, "uuid": uuid7()}
        file_uuid = uuid7()
        recording = MagicMock(files=[MagicMock(uuid=file_uuid)])
        recount = AsyncMock()
        monkeypatch.setattr(dives_module, "enforce_rate_limit", AsyncMock())
        monkeypatch.setattr(dives_module, "_get_owned_dive", AsyncMock(return_value=MagicMock(id=3)))
        monkeypatch.setattr(
            dives_module, "store_recording_file", AsyncMock(return_value=MagicMock(file_uuid=file_uuid))
        )
        monkeypatch.setattr(dives_module, "get_recordings_for_dives", AsyncMock(return_value={3: [recording]}))
        monkeypatch.setattr(dives_module, "recalculate_gear_dive_counts", recount)

        await dives_module.write_dive_recording(
            request=MagicMock(), uuid=uuid7(), current_user=owner, db=MagicMock(), file=MagicMock(), file_token="t"
        )

        recount.assert_awaited_once()
        assert recount.await_args is not None and recount.await_args.kwargs["user_id"] == 7
        dives_module.invalidate_gear_caches.assert_awaited_once_with(7)  # type: ignore[attr-defined]


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheLinkThroughTheRoutes:
    @pytest.mark.asyncio
    async def test_a_tank_links_its_vessel_and_the_read_and_the_counts_follow(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        vessel = _vessel(db, diver)
        vessel_id, vessel_uuid = vessel.id, vessel.uuid

        created = await _write(async_db, diver, mixtures=[{"volume": 11.1, "gear_item_uuid": str(vessel_uuid)}])

        (tank,) = created.mixtures
        assert tank.gear_item is not None
        assert (tank.gear_item.uuid, tank.gear_item.volume, tank.gear_item.material) == (vessel_uuid, 11.1, "aluminium")
        item = await async_db.get(GearItem, vessel_id, populate_existing=True)
        assert item is not None and (item.dive_count, item.dive_time) == (1, 2700)
        assert (await async_db.scalars(select(Dive.uuid).where(with_gear_item(vessel_id)))).all() == [created.uuid]

        await dives_module.erase_dive(request=MagicMock(), uuid=created.uuid, current_user=_as(diver), db=async_db)

        item = await async_db.get(GearItem, vessel_id, populate_existing=True)
        assert item is not None and (item.dive_count, item.dive_time) == (0, 0)

    @pytest.mark.asyncio
    async def test_a_vessel_also_listed_in_the_gear_counts_its_dive_once(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        vessel = _vessel(db, diver)
        vessel_id, vessel_uuid = vessel.id, str(vessel.uuid)

        await _write(async_db, diver, gear_item_uuids=[vessel_uuid], mixtures=[{"gear_item_uuid": vessel_uuid}])

        item = await async_db.get(GearItem, vessel_id, populate_existing=True)
        assert item is not None and (item.dive_count, item.dive_time) == (1, 2700)

    @pytest.mark.asyncio
    async def test_a_new_link_needs_a_cylinder_or_an_untyped_item(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        regulator, untyped, theirs = (
            create_gear_item(db, diver),
            _vessel(db, diver, gear_type=None),
            _vessel(db, other_diver),
        )
        regulator_uuid, untyped_uuid, theirs_uuid = str(regulator.uuid), str(untyped.uuid), str(theirs.uuid)

        with pytest.raises(UnprocessableEntityException, match="is not a cylinder"):
            await _write(async_db, diver, mixtures=[{"gear_item_uuid": regulator_uuid}])
        with pytest.raises(UnprocessableEntityException, match="Gear item not found."):
            await _write(async_db, diver, mixtures=[{"gear_item_uuid": theirs_uuid}])
        created = await _write(async_db, diver, mixtures=[{"gear_item_uuid": untyped_uuid}])

        assert created.mixtures[0].gear_item.uuid == untyped.uuid

    @pytest.mark.asyncio
    async def test_a_standing_link_survives_a_retype_and_moves_to_another_tank(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """The rows keep no identity across the replace, so a carried link is judged by item,
        never by position: moving a retyped vessel's link to the other tank still saves."""
        vessel, other = _vessel(db, diver), create_gear_item(db, diver)
        vessel_id, vessel_uuid, other_uuid = vessel.id, str(vessel.uuid), str(other.uuid)
        created = await _write(async_db, diver, mixtures=[{"gear_item_uuid": vessel_uuid}, {"oxygen": 50.0}])
        dive_id = (await async_db.scalars(select(Dive.id).where(Dive.uuid == created.uuid))).one()
        await async_db.execute(update(GearItem).where(GearItem.id == vessel_id).values(type="regulator"))
        await async_db.commit()

        await _patch(async_db, diver, created.uuid, mixtures=[{}, {"oxygen": 50.0, "gear_item_uuid": vessel_uuid}])
        await _patch(async_db, diver, created.uuid, notes="a notes edit")

        assert await _links(async_db, dive_id) == [None, vessel_id]
        with pytest.raises(UnprocessableEntityException, match="is not a cylinder"):
            await _patch(async_db, diver, created.uuid, mixtures=[{"gear_item_uuid": other_uuid}])

    @pytest.mark.asyncio
    async def test_the_table_refuses_a_repeated_vessel_with_a_message(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        vessel, dive = _vessel(db, diver), create_dive(db, diver)
        db.add_all(
            [DiveMixture(dive_id=dive.id, gear_item_id=vessel.id), DiveMixture(dive_id=dive.id, gear_item_id=vessel.id)]
        )

        with pytest.raises(IntegrityError) as caught:
            db.commit()
        db.rollback()

        assert (
            dives_module._mixture_error_detail(caught.value) == "A gear item can be linked from one cylinder per dive."
        )


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheVessel:
    @pytest.mark.asyncio
    async def test_a_capacity_correction_rewrites_the_linked_tanks_and_a_clear_leaves_them(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        vessel = _vessel(db, diver)
        linked, unlinked = create_dive(db, diver), create_dive(db, diver)
        db.add_all(
            [
                DiveMixture(dive_id=linked.id, volume=11.1, gear_item_id=vessel.id),
                DiveMixture(dive_id=unlinked.id, volume=11.1),
            ]
        )
        db.commit()
        vessel_uuid, linked_id, unlinked_id = vessel.uuid, linked.id, unlinked.id

        async def volumes() -> tuple[float | None, float | None]:
            async_db.expire_all()
            by_dive = dict((await async_db.execute(select(DiveMixture.dive_id, DiveMixture.volume))).tuples().all())
            return by_dive[linked_id], by_dive[unlinked_id]

        for change, expected in (({"volume": 12.0}, (12.0, 11.1)), ({"volume": None}, (12.0, 11.1))):
            await gear_items_module.patch_gear_item(
                request=MagicMock(),
                uuid=vessel_uuid,
                values=GearItemUpdate.model_validate(change),
                current_user=_as(diver),
                db=async_db,
            )
            assert await volumes() == expected

    @pytest.mark.asyncio
    async def test_deleting_the_vessel_unlinks_its_tanks_and_keeps_them(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        vessel, dive = _vessel(db, diver), create_dive(db, diver)
        db.add(DiveMixture(dive_id=dive.id, volume=12.0, gear_item_id=vessel.id))
        db.commit()
        vessel_uuid, dive_id = vessel.uuid, dive.id

        await gear_items_module.erase_gear_item(
            request=MagicMock(), uuid=vessel_uuid, current_user=_as(diver), db=async_db
        )

        rows = (
            await async_db.execute(
                select(DiveMixture.volume, DiveMixture.gear_item_id).where(DiveMixture.dive_id == dive_id)
            )
        ).all()
        assert [tuple(row) for row in rows] == [(12.0, None)]


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheHoursArm:
    @pytest.mark.asyncio
    async def test_the_rule_and_the_service_snapshot_the_item_s_dive_time(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        vessel = _vessel(db, diver)
        vessel_uuid = vessel.uuid
        await _write(async_db, diver, mixtures=[{"gear_item_uuid": str(vessel_uuid)}])

        schedule = await gear_service_module.write_gear_service_schedule(
            request=MagicMock(),
            schedule=GearServiceScheduleCreate(
                gear_item_uuid=vessel_uuid, kind="other", label="Valve", starts_on=date(2026, 1, 1), interval_hours=1
            ),
            current_user=_as(diver),
            db=async_db,
        )
        assert (schedule.dive_time_at_start, schedule.next_due_at_dive_time) == (2700, 6300)

        await _write(async_db, diver, mixtures=[{"gear_item_uuid": str(vessel_uuid)}])
        record = await gear_service_module.write_gear_service_record(
            request=MagicMock(),
            record=GearServiceRecordCreate(
                gear_item_uuid=vessel_uuid, kind="other", label="Valve", serviced_on=date(2026, 6, 2)
            ),
            current_user=_as(diver),
            db=async_db,
        )
        assert record.dive_time_at_service == 5400
        stored = (
            await async_db.scalars(
                select(GearServiceSchedule)
                .where(GearServiceSchedule.uuid == schedule.uuid)
                .execution_options(populate_existing=True)
            )
        ).one()
        assert stored.next_due_at_dive_time == 5400 + 3600

    @pytest.mark.asyncio
    async def test_a_patch_may_leave_the_hours_arm_alone_and_not_every_arm_empty(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        vessel = _vessel(db, diver)
        created = await gear_service_module.write_gear_service_schedule(
            request=MagicMock(),
            schedule=GearServiceScheduleCreate(
                gear_item_uuid=vessel.uuid, kind="service", starts_on=date(2026, 1, 1), interval_months=12
            ),
            current_user=_as(diver),
            db=async_db,
        )

        async def patch(**values: Any) -> None:
            await gear_service_module.patch_gear_service_schedule(
                request=MagicMock(),
                uuid=created.uuid,
                values=GearServiceScheduleUpdate.model_validate(values),
                current_user=_as(diver),
                db=async_db,
            )

        await patch(interval_hours=40, interval_months=None)
        with pytest.raises(UnprocessableEntityException, match=NEEDS_AN_INTERVAL):
            await patch(interval_hours=None)


def _cylinder_logbook(db: Session) -> User:
    """A diver whose steel 12 is linked from a dive's tank, with an hours rule and a service."""
    user = create_user(db)
    vessel = _vessel(db, user, volume=12.0)
    vessel.material, vessel.working_pressure = "steel", 232.0
    dive = create_dive(db, user)
    db.add(
        DiveMixture(dive_id=dive.id, volume=12.0, oxygen=32.0, helium=0.0, start_pressure=200.0, gear_item_id=vessel.id)
    )
    db.commit()
    schedule = GearServiceSchedule(
        user_id=user.id,
        gear_item_id=vessel.id,
        kind="other",
        label="Valve",
        starts_on=date(2026, 1, 1),
        interval_hours=1,
        dive_time_at_start=0,
    )
    db.add(schedule)
    db.commit()
    db.add(
        GearServiceRecord(
            user_id=user.id,
            gear_item_id=vessel.id,
            gear_service_schedule_id=schedule.id,
            kind="other",
            label="Valve",
            serviced_on=date(2026, 6, 2),
            dive_count_at_service=1,
            dive_time_at_service=1800,
            notes="",
        )
    )
    db.execute(update(GearItem).where(GearItem.id == vessel.id).values(dive_count=1, dive_time=1800))
    db.commit()
    return user


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheExportAndImport:
    @pytest.mark.asyncio
    async def test_the_export_carries_every_new_member_and_validates(self, db: Session, async_db: AsyncSession) -> None:
        user = _cylinder_logbook(db)

        document = json.loads(await _export(async_db, user.id))

        assert divejson.validate_document(document) == []
        (item,) = document["gear"]
        assert (item["volume"], item["working_pressure"], item["material"], item["dive_time"]) == (
            12.0,
            232.0,
            "steel",
            1800,
        )
        assert document["dives"][0]["cylinders"][0]["gear_uuid"] == item["uuid"]
        (schedule,) = document["gear_service_schedules"]
        assert (schedule["interval_hours"], schedule["dive_time_at_start"]) == (1, 0)
        assert document["gear_service_records"][0]["dive_time_at_service"] == 1800

    @pytest.mark.asyncio
    async def test_a_restore_keeps_the_link_and_the_facts_and_recomputes_the_time(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        source = _cylinder_logbook(db)
        document = await _export(async_db, source.id)
        destination = create_user(db)

        await _apply(async_db, destination.id, document)

        item = (await async_db.scalars(select(GearItem).where(GearItem.user_id == destination.id))).one()
        assert (item.volume, item.working_pressure, item.material, item.dive_count, item.dive_time) == (
            12.0,
            232.0,
            "steel",
            1,
            1800,
        )
        assert (
            await async_db.scalars(select(DiveMixture.gear_item_id).join(Dive).where(Dive.user_id == destination.id))
        ).all() == [item.id]
        schedule = (
            await async_db.scalars(select(GearServiceSchedule).where(GearServiceSchedule.user_id == destination.id))
        ).one()
        assert (schedule.interval_hours, schedule.next_due_at_dive_time) == (1, 1800 + 3600)

    @pytest.mark.asyncio
    async def test_a_restore_keeps_a_link_to_a_vessel_retyped_since(self, db: Session, async_db: AsyncSession) -> None:
        source = _cylinder_logbook(db)
        db.execute(update(GearItem).where(GearItem.user_id == source.id).values(type="regulator"))
        db.commit()
        destination = create_user(db)

        await _apply(async_db, destination.id, await _export(async_db, source.id))

        item = (await async_db.scalars(select(GearItem).where(GearItem.user_id == destination.id))).one()
        assert (
            await async_db.scalars(select(DiveMixture.gear_item_id).join(Dive).where(Dive.user_id == destination.id))
        ).all() == [item.id]

    @pytest.mark.asyncio
    async def test_absent_time_snapshots_are_reconstructed_and_reported(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        source = _cylinder_logbook(db)
        document = json.loads(await _export(async_db, source.id))
        del document["gear_service_schedules"][0]["dive_time_at_start"]
        del document["gear_service_records"][0]["dive_time_at_service"]
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(document).encode())

        # The dive was created before both rows, so each reconstructs to its 1800 s.
        schedule = (
            await async_db.scalars(select(GearServiceSchedule).where(GearServiceSchedule.user_id == destination.id))
        ).one()
        record = (
            await async_db.scalars(select(GearServiceRecord).where(GearServiceRecord.user_id == destination.id))
        ).one()
        assert (schedule.dive_time_at_start, record.dive_time_at_service) == (1800, 1800)
        assert schedule.next_due_at_dive_time == 1800 + 3600
        derived = [
            note for note in plan.notes if note.code is ImportNoteCode.VALUE_DERIVED and "dive time" in note.message
        ]
        assert {note.collection for note in derived} == {"gear_service_schedules", "gear_service_records"}

    @pytest.mark.asyncio
    async def test_two_cylinders_naming_one_vessel_keep_their_gas_and_one_link(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """Two distinct document uuids for one item collapse onto one row; the second
        cylinder keeps its gas and loses the link rather than the index refusing the import."""
        source = _cylinder_logbook(db)
        document = json.loads(await _export(async_db, source.id))
        (item,) = document["gear"]
        alias = {**item, "uuid": str(uuid7())}
        document["gear"].append(alias)
        cylinders = document["dives"][0]["cylinders"]
        cylinders.append({**cylinders[0], "gear_uuid": alias["uuid"], "oxygen": 50.0, "gas_number": 1})
        destination = create_user(db)

        plan = await _apply(async_db, destination.id, json.dumps(document).encode())

        rows = (
            await async_db.execute(
                select(DiveMixture.oxygen, DiveMixture.gear_item_id)
                .join(Dive)
                .where(Dive.user_id == destination.id)
                .order_by(DiveMixture.id)
            )
        ).all()
        vessel_id = (await async_db.scalars(select(GearItem.id).where(GearItem.user_id == destination.id))).one()
        assert [tuple(row) for row in rows] == [(32.0, vessel_id), (50.0, None)]
        assert any("already links" in note.message for note in plan.notes)
