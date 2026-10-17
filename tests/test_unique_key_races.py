"""A name another request takes between a route's `unique_key_taken` probe and its write is the
probe's own 422, not a 500 from the unique index. Simulated by a probe that answers "free" for a
name that is already stored - the window a concurrent double-POST opens."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.api.v1 import contacts as contacts_module
from src.app.api.v1 import dive_form_presets as dive_form_presets_module
from src.app.api.v1 import dive_sites as dive_sites_module
from src.app.api.v1 import gear_items as gear_items_module
from src.app.api.v1 import gear_service as gear_service_module
from src.app.api.v1 import gear_sets as gear_sets_module
from src.app.api.v1 import people as people_module
from src.app.api.v1 import tags as tags_module
from src.app.api.v1 import trips as trips_module
from src.app.core.exceptions.http_exceptions import DuplicateValueException
from src.app.models.tag import Tag
from src.app.models.user import User
from src.app.schemas.contact import ContactCreate, ContactUpdateRequest
from src.app.schemas.dive_form_preset import DiveFormPresetCreate, DiveFormPresetUpdate
from src.app.schemas.dive_site import DiveSiteCreate, DiveSiteUpdateRequest
from src.app.schemas.gear_item import GearItemCreate, GearItemUpdate
from src.app.schemas.gear_service import GearServiceScheduleCreate, GearServiceScheduleUpdate
from src.app.schemas.gear_set import GearSetCreateRequest, GearSetUpdateRequest
from src.app.schemas.person import PersonCreate, PersonUpdateRequest
from src.app.schemas.tag import TagUpdate
from src.app.schemas.trip import TripCreate, TripUpdateRequest
from tests.conftest import db_available

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

MODULES = (
    contacts_module,
    dive_form_presets_module,
    dive_sites_module,
    gear_items_module,
    gear_service_module,
    gear_sets_module,
    people_module,
    tags_module,
    trips_module,
)


@pytest.fixture(autouse=True)
def no_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    for module in MODULES:
        for name in dir(module):
            if name.startswith("invalidate_") and name.endswith("_caches"):
                monkeypatch.setattr(module, name, AsyncMock())


def _as(user: User) -> dict[str, Any]:
    return {"id": user.id, "uuid": user.uuid, "username": user.username, "is_superuser": False}


async def _gear_item(async_db: AsyncSession, diver: User, name: str = "Regulator") -> Any:
    return await gear_items_module.write_gear_item(
        request=MagicMock(), gear_item=GearItemCreate(name=name), current_user=_as(diver), db=async_db
    )


@dataclass(frozen=True)
class Resource:
    """One route pair guarded by a probe: how to create a row under a key, and how to rename
    another onto it."""

    module: ModuleType
    probe: str
    create: Callable[[AsyncSession, User, str], Awaitable[Any]]
    rename: Callable[[AsyncSession, User, Any, str], Awaitable[Any]]

    def __repr__(self) -> str:
        return self.module.__name__.rsplit(".", 1)[-1]


def _routes(module: ModuleType, create: str, body: str, patch: str, schema: Any, update: Any) -> tuple[Any, Any]:
    async def write(async_db: AsyncSession, diver: User, name: str) -> Any:
        return await getattr(module, create)(
            request=MagicMock(), current_user=_as(diver), db=async_db, **{body: schema.model_validate({"name": name})}
        )

    async def rename(async_db: AsyncSession, diver: User, row: Any, name: str) -> Any:
        return await getattr(module, patch)(
            request=MagicMock(),
            uuid=row.uuid,
            values=update.model_validate({"name": name}),
            current_user=_as(diver),
            db=async_db,
        )

    return write, rename


async def _write_schedule(async_db: AsyncSession, diver: User, label: str) -> Any:
    item = await _gear_item(async_db, diver, name=f"Regulator {label}")
    return await _schedule_on(async_db, diver, item.uuid, label)


async def _schedule_on(async_db: AsyncSession, diver: User, gear_item_uuid: Any, label: str) -> Any:
    return await gear_service_module.write_gear_service_schedule(
        request=MagicMock(),
        schedule=GearServiceScheduleCreate(
            gear_item_uuid=gear_item_uuid, kind="other", label=label, starts_on=date(2026, 1, 1), interval_months=12
        ),
        current_user=_as(diver),
        db=async_db,
    )


async def _relabel_schedule(async_db: AsyncSession, diver: User, row: Any, label: str) -> Any:
    return await gear_service_module.patch_gear_service_schedule(
        request=MagicMock(),
        uuid=row.uuid,
        values=GearServiceScheduleUpdate(label=label),
        current_user=_as(diver),
        db=async_db,
    )


RESOURCES = [
    Resource(
        contacts_module,
        "contact_name_exists",
        *_routes(contacts_module, "write_contact", "contact", "patch_contact", ContactCreate, ContactUpdateRequest),
    ),
    Resource(
        people_module,
        "person_name_exists",
        *_routes(people_module, "write_person", "person", "patch_person", PersonCreate, PersonUpdateRequest),
    ),
    Resource(
        gear_items_module,
        "gear_item_name_exists",
        *_routes(gear_items_module, "write_gear_item", "gear_item", "patch_gear_item", GearItemCreate, GearItemUpdate),
    ),
    Resource(
        gear_sets_module,
        "gear_set_name_exists",
        *_routes(
            gear_sets_module, "write_gear_set", "gear_set", "patch_gear_set", GearSetCreateRequest, GearSetUpdateRequest
        ),
    ),
    Resource(
        dive_sites_module,
        "dive_site_name_exists",
        *_routes(
            dive_sites_module, "write_dive_site", "dive_site", "patch_dive_site", DiveSiteCreate, DiveSiteUpdateRequest
        ),
    ),
    Resource(
        dive_form_presets_module,
        "dive_form_preset_name_exists",
        *_routes(
            dive_form_presets_module,
            "write_dive_form_preset",
            "preset",
            "patch_dive_form_preset",
            DiveFormPresetCreate,
            DiveFormPresetUpdate,
        ),
    ),
    Resource(
        trips_module,
        "trip_name_exists",
        *_routes(trips_module, "write_trip", "trip", "patch_trip", TripCreate, TripUpdateRequest),
    ),
    Resource(gear_service_module, "schedule_kind_exists", _write_schedule, _relabel_schedule),
]


class TestACreateThatLosesTheRace:
    @pytest.mark.asyncio
    async def test_a_schedule_on_a_taken_slot_is_the_probes_422(
        self, monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession, diver: User
    ) -> None:
        """A schedule's key is its item's, so the second create has to name the same item."""
        item = await _gear_item(async_db, diver)
        await _schedule_on(async_db, diver, item.uuid, "Valve")
        monkeypatch.setattr(gear_service_module, "schedule_kind_exists", AsyncMock(return_value=False))

        with pytest.raises(DuplicateValueException, match="already has a schedule"):
            await _schedule_on(async_db, diver, item.uuid, "VALVE")

    @pytest.mark.parametrize("resource", [r for r in RESOURCES if r.module is not gear_service_module], ids=repr)
    @pytest.mark.asyncio
    async def test_it_is_the_probes_422_and_the_session_is_usable(
        self, resource: Resource, monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession, diver: User
    ) -> None:
        await resource.create(async_db, diver, "Blue Hole")
        monkeypatch.setattr(resource.module, resource.probe, AsyncMock(return_value=False))

        with pytest.raises(DuplicateValueException, match="already exists"):
            await resource.create(async_db, diver, "BLUE HOLE")

        # Rolled back, so the request's session answers the next statement instead of refusing it.
        assert await async_db.scalar(select(func.count()).select_from(User).where(User.id == diver.id)) == 1


class TestARenameThatLosesTheRace:
    @pytest.mark.parametrize("resource", RESOURCES, ids=repr)
    @pytest.mark.asyncio
    async def test_it_is_the_probes_422(
        self, resource: Resource, monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession, diver: User
    ) -> None:
        if resource.module is gear_service_module:
            item = await _gear_item(async_db, diver)
            await _schedule_on(async_db, diver, item.uuid, "Valve")
            other = await _schedule_on(async_db, diver, item.uuid, "Hose")
        else:
            await resource.create(async_db, diver, "Blue Hole")
            other = await resource.create(async_db, diver, "Canyon")
        monkeypatch.setattr(resource.module, resource.probe, AsyncMock(return_value=False))

        with pytest.raises(DuplicateValueException):
            await resource.rename(
                async_db, diver, other, "Valve" if resource.module is gear_service_module else "BLUE HOLE"
            )

    @pytest.mark.asyncio
    async def test_a_tag_renamed_onto_a_taken_name_is_the_probes_422(
        self, monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession, diver: User
    ) -> None:
        async_db.add_all([Tag(user_id=diver.id, name="Night"), Tag(user_id=diver.id, name="Wall")])
        await async_db.commit()
        wall = await async_db.scalar(select(Tag).where(Tag.user_id == diver.id, Tag.name == "Wall"))
        assert wall is not None
        monkeypatch.setattr(tags_module, "tag_name_exists", AsyncMock(return_value=False))

        with pytest.raises(DuplicateValueException, match="already exists"):
            await tags_module.patch_tag(
                request=MagicMock(),
                uuid=wall.uuid,
                values=TagUpdate(name="NIGHT"),
                current_user=_as(diver),
                db=async_db,
            )
