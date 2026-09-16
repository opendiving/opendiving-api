"""The delete itself, against a live Postgres: the row is gone, and so is what pointed
at it.

`test_export_loader.py` pins the *consequences* of the cascades, because export is where
a dangling reference used to hurt. This pins the two facts underneath them, neither of
which any other test states.

The first is that `crud_X.delete` really issues a `DELETE`. FastCRUD branches on whether
the model carries `is_deleted` and silently flags the row instead when it does, so
re-adding `SoftDeleteMixin` to any hard-deleting resource - or copying one of them into a
new model that then never gets a case here - would turn every cascade in this change back
off. The cases below call the CRUD layer rather than the route deliberately: the route
tests all stub `delete`, which is exactly the layer in question.

The second is that a name frees its slot. The `ux_*` indexes behind these resources were
partial on `is_deleted` so a diver could reuse a deleted site's name; hard delete gives
that for free, and the app-level `*_name_exists` checks in front of them have to agree.
Not every resource has such a slot - `Course` deliberately has no per-user unique name, a
course retaken later being legitimately the same name twice - so `name_exists` is optional
and `TestTheRegistryIsComplete` derives which entries may leave it out.

`HARD_DELETED_RESOURCES` is what all three of those classes run over, and
`TestTheRegistryIsComplete` is what keeps it honest - it reads the models rather than a
list written here, needs no database, and is the only thing in this file that fails when
a new hard-deleting resource arrives with no case.

Everything else here skips when no database is reachable. On a developer's machine that
means `POSTGRES_SERVER=localhost` (`src/.env` points at the compose hostname, which does
not resolve on the host); CI sets it and fails the job if anything skips. See
CONTRIBUTING.md.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

import pytest
from sqlalchemy import Table, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.api.dependencies import fetch_owned_or_raise
from src.app.api.v1.gear_service import _owned_gear_item
from src.app.core.db.database import Base
from src.app.core.exceptions.http_exceptions import NotFoundException
from src.app.crud.crud_courses import crud_courses
from src.app.crud.crud_dive_form_presets import crud_dive_form_presets, dive_form_preset_name_exists
from src.app.crud.crud_dive_sites import crud_dive_sites, dive_site_name_exists
from src.app.crud.crud_gear_items import crud_gear_items, gear_item_name_exists
from src.app.crud.crud_gear_service_records import crud_gear_service_records
from src.app.crud.crud_gear_service_schedules import (
    crud_gear_service_schedules,
    resolve_schedule_for_user,
    schedule_kind_exists,
)
from src.app.crud.crud_gear_sets import crud_gear_sets, gear_set_name_exists
from src.app.crud.crud_trips import crud_trips, trip_name_exists
from src.app.models.course import Course
from src.app.models.dive_form_preset import DiveFormPreset
from src.app.models.dive_site import DiveSite
from src.app.models.gear_item import GearItem
from src.app.models.gear_service_schedule import GearServiceSchedule
from src.app.models.gear_set import GearSet
from src.app.models.trip import Trip
from src.app.models.user import User
from src.app.schemas.course import CourseReadInternal
from src.app.schemas.dive_form_preset import DiveFormPresetReadInternal
from src.app.schemas.dive_site import DiveSiteReadInternal
from src.app.schemas.gear_item import GearItemReadInternal
from src.app.schemas.gear_set import GearSetReadInternal
from src.app.schemas.trip import TripReadInternal
from tests.conftest import db_available
from tests.helpers.generators import (
    create_course,
    create_dive_form_preset,
    create_dive_site,
    create_gear_item,
    create_gear_service_record,
    create_gear_service_schedule,
    create_gear_set,
    create_trip,
)
from tests.helpers.model_metadata import (
    NOT_A_DIVERS_OWN_RESOURCE,
    addressable_hard_deleted,
    divers_own_hard_deleted,
    soft_deleting_models,
)

needs_a_database = pytest.mark.skipif(not db_available(), reason="No database connection available")


@dataclass(frozen=True)
class Resource:
    """One hard-deleting resource, reduced to the four things a case needs of it.

    Each entry still takes per-resource judgement to write - the generators differ, and the
    `*_name_exists` helpers each take a different set of keyword arguments - which is why
    this is a table rather than another derivation. What the table buys is that leaving a
    resource out of it is a failure (`TestTheRegistryIsComplete`) rather than silence.
    """

    crud: Any
    create: Callable[[Session, User], Any]
    resolve: Callable[[AsyncSession, User, Any], Awaitable[Any]]
    # `None` for a resource with no per-user unique slot to free, which is a real design
    # position rather than an omission - see `TestTheRegistryIsComplete`, which derives who
    # may say `None` from the models' own indexes so it cannot be used to skip a case.
    name_exists: Callable[[AsyncSession, User, Any], Awaitable[bool]] | None = None


def _resolves_through_fetch_owned(crud: Any, schema: type) -> Callable[[AsyncSession, User, Any], Awaitable[Any]]:
    """How every one of these routes resolves the row before doing anything else."""

    async def resolve(session: AsyncSession, diver: User, row: Any) -> Any:
        return await fetch_owned_or_raise(
            db=session,
            crud=crud,
            uuid=row.uuid,
            current_user={"id": diver.id, "uuid": diver.uuid},
            schema=schema,
            not_found_message="Not found",
        )

    return resolve


async def _resolve_schedule(session: AsyncSession, diver: User, row: Any) -> Any:
    """The schedule route resolves through `resolve_schedule_for_user` rather than
    `fetch_owned_or_raise`, and turns its `None` into the same `NotFoundException`
    (`api/v1/gear_service.py`) - so it reaches the shared case in the shape the others do.
    """
    schedule = await resolve_schedule_for_user(db=session, schedule_uuid=row.uuid, user_id=diver.id)
    if schedule is None:
        raise NotFoundException("Service schedule not found")
    return schedule


HARD_DELETED_RESOURCES: dict[type[Base], Resource] = {
    Course: Resource(
        crud=crud_courses,
        create=create_course,
        resolve=_resolves_through_fetch_owned(crud_courses, CourseReadInternal),
        # No `name_exists`, and no `course_name_exists` helper to point it at: `course`
        # carries no per-user unique index, because a course failed once and retaken later
        # is legitimately the same name twice. The same reasoning that left `certification`
        # without one.
    ),
    Trip: Resource(
        crud=crud_trips,
        create=create_trip,
        resolve=_resolves_through_fetch_owned(crud_trips, TripReadInternal),
        name_exists=lambda session, diver, row: trip_name_exists(session, user_id=diver.id, name=row.name),
    ),
    DiveSite: Resource(
        crud=crud_dive_sites,
        create=create_dive_site,
        resolve=_resolves_through_fetch_owned(crud_dive_sites, DiveSiteReadInternal),
        name_exists=lambda session, diver, row: dive_site_name_exists(
            session, user_id=diver.id, name=row.name, location=row.location
        ),
    ),
    GearItem: Resource(
        crud=crud_gear_items,
        create=create_gear_item,
        resolve=_resolves_through_fetch_owned(crud_gear_items, GearItemReadInternal),
        name_exists=lambda session, diver, row: gear_item_name_exists(
            session, user_id=diver.id, name=row.name, brand=row.brand
        ),
    ),
    DiveFormPreset: Resource(
        crud=crud_dive_form_presets,
        create=create_dive_form_preset,
        resolve=_resolves_through_fetch_owned(crud_dive_form_presets, DiveFormPresetReadInternal),
        name_exists=lambda session, diver, row: dive_form_preset_name_exists(session, user_id=diver.id, name=row.name),
    ),
    GearSet: Resource(
        crud=crud_gear_sets,
        create=create_gear_set,
        resolve=_resolves_through_fetch_owned(crud_gear_sets, GearSetReadInternal),
        name_exists=lambda session, diver, row: gear_set_name_exists(session, user_id=diver.id, name=row.name),
    ),
    GearServiceSchedule: Resource(
        crud=crud_gear_service_schedules,
        # A schedule hangs off a gear item, so its generator needs one first.
        create=lambda db, diver: create_gear_service_schedule(db, diver, create_gear_item(db, diver)),
        resolve=_resolve_schedule,
        # A schedule's unique slot is `(gear_item_id, kind)`, not a name on the diver.
        name_exists=lambda session, diver, row: schedule_kind_exists(
            session, gear_item_id=row.gear_item_id, kind=row.kind
        ),
    ),
}


def _sorted_registrations() -> list[tuple[type[Base], Resource]]:
    return sorted(HARD_DELETED_RESOURCES.items(), key=lambda entry: entry[0].__name__)


each_resource = pytest.mark.parametrize(
    "resource",
    [pytest.param(resource, id=model.__name__) for model, resource in _sorted_registrations()],
)

# The name-slot class alone runs over a subset: a resource with no per-user unique index has
# no slot to free, so there is nothing for it to assert. Which resources may sit this out is
# not this list's decision - `TestTheRegistryIsComplete` derives it from the models.
each_resource_with_a_name_slot = pytest.mark.parametrize(
    "resource",
    [
        pytest.param(resource, id=model.__name__)
        for model, resource in _sorted_registrations()
        if resource.name_exists is not None
    ],
)


def _declares_a_natural_key(model: type[Base]) -> bool:
    """Whether the model has a unique index over something other than its public `uuid`.

    That is what a `*_name_exists` helper is the friendly-422 half of. Every model here has
    a unique `ix_*_uuid`, so the uuid one is filtered out rather than counted - keying on
    "has any unique index" would say every resource has a name slot and quietly make the
    exemption below unreachable.
    """
    # `cast` because `__table__` is typed `FromClause`, which carries `columns` but not
    # `indexes` - the mapped attribute really is a `Table`.
    table = cast(Table, model.__table__)
    return any(index.unique and {column.name for column in index.columns} != {"uuid"} for index in table.indexes)


async def _count(async_db: AsyncSession, model: Any, row_id: int) -> int:
    result = await async_db.execute(select(func.count()).select_from(model).where(model.id == row_id))
    return int(result.scalar_one())


class TestTheRegistryIsComplete:
    """No database: this is the models' own account of which resources hard-delete.

    The three classes below are the behaviour, and none of them can notice a resource that
    was never registered - a new model copied from `GearSet`, hard-deleting like it, simply
    has no case and every test still passes. This is the half that fails, in both
    directions, against `tests/helpers/model_metadata.py`.
    """

    def test_every_hard_deleting_resource_has_a_registration(self) -> None:
        """Add the model to `HARD_DELETED_RESOURCES` and the three classes below cover it;
        add it to `NOT_A_DIVERS_OWN_RESOURCE`, with the reason, if it has no delete of its
        own. Leaving it in neither is what this refuses.
        """
        unregistered = divers_own_hard_deleted() - set(HARD_DELETED_RESOURCES)

        assert sorted(model.__name__ for model in unregistered) == []

    def test_nothing_is_registered_that_the_models_no_longer_call_a_resource(self) -> None:
        """The same check from the other end. An entry left here for a model that has since
        been renamed, removed or made a child of something else would go on passing every
        case below against a resource the app no longer has.
        """
        stale = set(HARD_DELETED_RESOURCES) - divers_own_hard_deleted()

        assert sorted(model.__name__ for model in stale) == []

    def test_no_exclusion_names_a_model_that_is_no_longer_addressable(self) -> None:
        """`NOT_A_DIVERS_OWN_RESOURCE` is keyed by class name, so a renamed model leaves a
        key matching nothing - which is not an error but an exemption that has stopped
        being read, and would go on excusing the renamed model from the check above.
        """
        dangling = set(NOT_A_DIVERS_OWN_RESOURCE) - {model.__name__ for model in addressable_hard_deleted()}

        assert sorted(dangling) == []

    def test_nothing_registered_here_has_gone_back_to_soft_deleting(self) -> None:
        """The other direction, and the cheap half of what `TestTheRowIsActuallyRemoved`
        proves: re-adding `SoftDeleteMixin` to one of these makes FastCRUD flag the row
        instead of removing it, and fails here without needing a database to say so.
        """
        resurrected = set(HARD_DELETED_RESOURCES) & soft_deleting_models()

        assert sorted(model.__name__ for model in resurrected) == []

    def test_only_a_resource_without_a_natural_key_may_omit_its_name_check(self) -> None:
        """`name_exists=None` is an exemption from `TestADeletedNameFreesItsSlot`, so it has
        to be derived rather than taken on trust - otherwise it is a way to skip a real case
        by leaving a field out, and the one thing that class pins would go untested for
        whichever resource did it.

        Both directions. A resource that declares a `ux_*` index and omits the check has
        silently dropped its coverage; one that supplies a check with no unique index behind
        it is asserting against a slot that was never exclusive, which passes and means
        nothing. If a `Course` ever does gain a unique name, this is what fails until the
        helper and the entry arrive with it.
        """
        exempt = {model.__name__ for model, resource in HARD_DELETED_RESOURCES.items() if resource.name_exists is None}
        without_a_natural_key = {
            model.__name__ for model in HARD_DELETED_RESOURCES if not _declares_a_natural_key(model)
        }

        assert exempt == without_a_natural_key

    def test_each_registration_points_at_the_crud_for_its_own_model(self) -> None:
        """A copy-pasted entry left pointing at the neighbour's `crud_*` would exercise that
        neighbour twice and the new resource not at all, with every case still green.
        """
        mismatched = {
            model.__name__: resource.crud.model.__name__
            for model, resource in HARD_DELETED_RESOURCES.items()
            if resource.crud.model is not model
        }

        assert mismatched == {}


@needs_a_database
class TestTheRowIsActuallyRemoved:
    """One case per resource, because each has its own `FastCRUD` instance and its own
    chance to be wired back to a soft-deleting model."""

    @each_resource
    @pytest.mark.asyncio
    async def test_deleting_it(self, resource: Resource, db: Session, async_db: AsyncSession, diver: User) -> None:
        row = resource.create(db, diver)

        await resource.crud.delete(db=async_db, uuid=row.uuid)

        assert await _count(async_db, resource.crud.model, row.id) == 0


@needs_a_database
class TestASecondDeleteIsA404:
    """`DELETE` is not idempotent, and a second call 404s, and that is a deliberate contract change.

    The idempotency insured against a half-failed multi-statement delete; a single
    statement in one transaction cannot half-fail. Every one of these routes resolves the
    row before doing anything else, so what a second call actually meets is the lookup
    below returning nothing - which is what makes "moves nothing on a retry" true rather
    than just untested.
    """

    @each_resource
    @pytest.mark.asyncio
    async def test_it_no_longer_resolves(
        self, resource: Resource, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        row = resource.create(db, diver)
        await resource.crud.delete(db=async_db, uuid=row.uuid)

        with pytest.raises(NotFoundException):
            await resource.resolve(async_db, diver, row)


@needs_a_database
class TestADeletedNameFreesItsSlot:
    """What the partial `ux_*` predicates used to buy, now had for free.

    Each of these `*_name_exists` helpers is the friendly-422 half of a real unique index,
    and each used to carry an `is_deleted IS false` matching the index's `postgresql_where`.
    Both halves lost it together; a helper that kept one would refuse a name the index
    would happily accept, which reads to the diver as "that name is taken" for a row that
    does not exist.

    Only the resources that *have* such a slot, which is not all of them - see
    `each_resource_with_a_name_slot`.
    """

    @each_resource_with_a_name_slot
    @pytest.mark.asyncio
    async def test_the_slot_is_free_again(
        self, resource: Resource, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        assert resource.name_exists is not None  # narrowed by the parametrize above
        row = resource.create(db, diver)
        assert await resource.name_exists(async_db, diver, row) is True

        await resource.crud.delete(db=async_db, uuid=row.uuid)

        assert await resource.name_exists(async_db, diver, row) is False


@needs_a_database
class TestArchivingIsTheNonDestructivePath:
    """The other half of the delete dialog's promise: "to keep it in your log **and its service
    history**, archive it instead".

    Deleting a gear item now destroys its service history, so archiving is not one of two ways to
    retire kit and go on reading its records - it is the only one. That makes the premise below
    worth strictly more than when *"A deleted gear item's service history has no view"* first
    recorded it, and it is one kwarg from being false: adding `is_archived=False` to
    `_owned_gear_item`, to match the listing filter, looks like an obvious tidy-up and would
    silently take the history off archived items too.
    """

    @pytest.mark.asyncio
    async def test_an_archived_item_still_resolves(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        item = create_gear_item(db, diver, is_archived=True)

        resolved = await _owned_gear_item(async_db, item.uuid, diver.id)

        assert resolved.id == item.id
        assert resolved.is_archived is True

    @pytest.mark.asyncio
    async def test_archiving_keeps_the_schedules_and_records(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """The contrast with `TestTheRowIsActuallyRemoved` is the whole point: same intent
        ("retire this"), opposite outcome for everything hanging off the item."""
        item = create_gear_item(db, diver)
        schedule = create_gear_service_schedule(db, diver, item)
        record = create_gear_service_record(db, diver, item, schedule=schedule)

        item.is_archived = True
        db.commit()

        assert await _count(async_db, GearServiceSchedule, schedule.id) == 1
        rows = await async_db.execute(
            select(crud_gear_service_records.model.gear_service_schedule_id).where(
                crud_gear_service_records.model.id == record.id
            )
        )
        assert rows.scalar_one() == schedule.id
