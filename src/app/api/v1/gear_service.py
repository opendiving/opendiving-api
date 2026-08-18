"""Service schedules and service records for gear.

Routes are flat (`/gear-service-schedule`, not `/gear-item/{uuid}/service-schedules`),
matching the two earlier flattenings recorded in DECISIONS.md: each resource has its own
`uuid` and nesting would give it a second identity. Filtering by item is a query
parameter, the same way `GET /dives?gear_item_uuid=` works.

Every route follows the "auth before cache" rule: an uncached lookup resolves the row
and checks ownership *first*, and only then is a `@cache`-decorated helper called - see
DECISIONS.md for why putting the check inside a cached function is a vulnerability.
"""

import uuid as uuid_pkg
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import (
    DuplicateValueException,
    ForbiddenException,
    NotFoundException,
    UnprocessableEntityException,
)
from ...core.utils.cache import cache
from ...core.utils.pagination import clamp_pagination
from ...crud.crud_gear_items import crud_gear_items, get_gear_item_uuids_by_id
from ...crud.crud_gear_service_records import (
    crud_gear_service_records,
    find_schedule_for_record,
    resolve_record_for_user,
)
from ...crud.crud_gear_service_schedules import (
    crud_gear_service_schedules,
    get_due_overview_for_user,
    resolve_schedule_for_user,
    schedule_kind_exists,
)
from ...schemas.gear_item import GearItemReadInternal
from ...schemas.gear_service import (
    GearServiceDueResponse,
    GearServiceRecordCreate,
    GearServiceRecordCreateInternal,
    GearServiceRecordRead,
    GearServiceRecordReadInternal,
    GearServiceRecordUpdate,
    GearServiceScheduleCreate,
    GearServiceScheduleCreateInternal,
    GearServiceScheduleRead,
    GearServiceScheduleReadInternal,
    GearServiceScheduleUpdate,
)
from ...services.cache_invalidation import invalidate_gear_caches
from ...services.gear_service import recalculate_service_schedule

router = APIRouter(tags=["gear"])

# `GET /gear-service-due` returns the user's whole active schedule list rather than a
# date-filtered slice (see `GearServiceDueResponse`), so it needs *some* bound. A diver
# with 200 live service rules has bigger problems than a truncated dashboard card.
DUE_OVERVIEW_LIMIT = 200


# -------------------- shape conversion --------------------
def _to_public_schedule(
    db_schedule: GearServiceScheduleReadInternal | dict[str, Any],
    *,
    user_uuid: uuid_pkg.UUID,
    gear_item_uuid: uuid_pkg.UUID,
) -> GearServiceScheduleRead:
    """Convert an internal schedule representation (integer FKs) into its public shape.

    Drops the internal ids and the `notified_*` bookkeeping, which is the digest job's
    private state and has no meaning to a client.
    """
    data = db_schedule if isinstance(db_schedule, dict) else db_schedule.model_dump()
    hidden = {"id", "user_id", "gear_item_id", "notified_stage", "notified_for_due_on"}
    hidden |= {"notified_for_due_at_dive_count", "notified_at"}
    return GearServiceScheduleRead(
        **{k: v for k, v in data.items() if k not in hidden},
        user_uuid=user_uuid,
        gear_item_uuid=gear_item_uuid,
    )


def _to_public_record(
    db_record: GearServiceRecordReadInternal | dict[str, Any],
    *,
    user_uuid: uuid_pkg.UUID,
    gear_item_uuid: uuid_pkg.UUID,
    gear_service_schedule_uuid: uuid_pkg.UUID | None,
) -> GearServiceRecordRead:
    """Convert an internal record representation (integer FKs) into its public shape."""
    data = db_record if isinstance(db_record, dict) else db_record.model_dump()
    hidden = {"id", "user_id", "gear_item_id", "gear_service_schedule_id"}
    return GearServiceRecordRead(
        **{k: v for k, v in data.items() if k not in hidden},
        user_uuid=user_uuid,
        gear_item_uuid=gear_item_uuid,
        gear_service_schedule_uuid=gear_service_schedule_uuid,
    )


async def _owned_gear_item(db: AsyncSession, gear_item_uuid: uuid_pkg.UUID, user_id: int) -> GearItemReadInternal:
    """Fetch a gear item the caller owns, or raise 422.

    422 rather than 403/404 mirrors `_resolve_item_ids` in `gear_sets.py`: from the
    caller's point of view this is a bad reference inside a request body, not a failed
    attempt to read a resource - and answering identically for "not yours" and "doesn't
    exist" keeps someone else's gear uuids unprobeable.
    """
    db_gear_item = await crud_gear_items.get(
        db=db,
        uuid=gear_item_uuid,
        user_id=user_id,
        schema_to_select=GearItemReadInternal,
        return_as_model=True,
    )
    if db_gear_item is None:
        raise UnprocessableEntityException("Gear item not found.")
    return cast(GearItemReadInternal, db_gear_item)


# -------------------- schedules --------------------
@router.post("/gear-service-schedule", response_model=GearServiceScheduleRead, status_code=201)
async def write_gear_service_schedule(
    request: Request,
    schedule: GearServiceScheduleCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> GearServiceScheduleRead:
    """Create a servicing rule for a gear item.

    Ownership comes from the item rather than a `user_uuid` in the body - strictly
    stronger, since the caller can't name an item that isn't theirs in the first place.
    """
    db_gear_item = await _owned_gear_item(db, schedule.gear_item_uuid, current_user["id"])

    if await schedule_kind_exists(db=db, gear_item_id=db_gear_item.id, kind=schedule.kind.value, label=schedule.label):
        raise DuplicateValueException("This gear item already has a schedule for that kind of service")

    schedule_internal = GearServiceScheduleCreateInternal(
        **schedule.model_dump(exclude={"gear_item_uuid"}),
        user_id=current_user["id"],
        gear_item_id=db_gear_item.id,
        # Snapshotted here rather than accepted from the client: it's the baseline a
        # dive-based interval counts up from, so a caller able to set it could move
        # their own due threshold arbitrarily.
        dive_count_at_start=db_gear_item.dive_count,
    )
    created = await crud_gear_service_schedules.create(
        db=db, object=schedule_internal, schema_to_select=GearServiceScheduleReadInternal, return_as_model=True
    )
    # A brand-new schedule has no records, so this derives the first due date straight
    # from `starts_on`/`dive_count_at_start`.
    await recalculate_service_schedule(db=db, schedule_id=created.id)
    await invalidate_gear_caches(current_user["id"])

    schedule_read = await crud_gear_service_schedules.get(
        db=db, id=created.id, schema_to_select=GearServiceScheduleReadInternal, return_as_model=True
    )
    if schedule_read is None:
        raise NotFoundException("Created service schedule not found")

    return _to_public_schedule(
        cast(GearServiceScheduleReadInternal, schedule_read),
        user_uuid=current_user["uuid"],
        gear_item_uuid=db_gear_item.uuid,
    )


@cache(
    key_prefix=(
        "user_{user_id}_gear_service_schedules:page_{page}:items_per_page:{items_per_page}:item_{gear_item_id}"
    ),
    resource_id_name="user_id",
    expiration=60,
)
async def _cached_read_schedules(
    request: Request,
    user_id: int,
    user_uuid: uuid_pkg.UUID,
    db: AsyncSession,
    page: int,
    items_per_page: int,
    gear_item_id: int | None,
) -> dict:
    """Fetches (and caches) a user's paginated service schedules.

    Like every other cached read helper here, this must only be called once the route
    has authorized the caller - `@cache` serves a hit without re-running any of the
    function body, authorization included.
    """
    filters: dict[str, Any] = {"user_id": user_id}
    if gear_item_id is not None:
        filters["gear_item_id"] = gear_item_id

    data = await crud_gear_service_schedules.get_multi(
        db=db,
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
        sort_columns=["next_due_on", "kind"],
        sort_orders=["asc", "asc"],
        **filters,
    )
    # One round trip for the whole page's items rather than one per row.
    uuid_by_id = await get_gear_item_uuids_by_id(db=db, gear_item_ids=[row["gear_item_id"] for row in data["data"]])
    # `.get()`-and-skip rather than indexing: this and the read above are two statements at
    # READ COMMITTED, so a `DELETE /gear-item/{uuid}` committing between them takes the
    # schedule with it (`ON DELETE CASCADE`) and leaves an id here that resolves to nothing.
    # Dropping the row is what a fresh read a moment later returns anyway; indexing would be
    # a `KeyError` and a 500. Unreachable while these five soft-deleted, since the row
    # survived - see `get_gear_item_uuids_by_id`.
    data["data"] = [
        _to_public_schedule(row, user_uuid=user_uuid, gear_item_uuid=item_uuid).model_dump()
        for row in data["data"]
        if (item_uuid := uuid_by_id.get(row["gear_item_id"])) is not None
    ]

    response: dict[str, Any] = paginated_response(crud_data=data, page=page, items_per_page=items_per_page)
    return response


@router.get("/gear-service-schedules", response_model=PaginatedListResponse[GearServiceScheduleRead])
async def read_gear_service_schedules(
    request: Request,
    user_uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    gear_item_uuid: uuid_pkg.UUID | None = None,
    page: int = 1,
    items_per_page: int = 10,
) -> dict:
    """List a user's service schedules, optionally narrowed to one gear item."""
    if current_user["uuid"] != user_uuid:
        raise ForbiddenException()

    page, items_per_page = clamp_pagination(page, items_per_page)

    gear_item_id = None
    if gear_item_uuid is not None:
        gear_item_id = (await _owned_gear_item(db, gear_item_uuid, current_user["id"])).id

    return await _cached_read_schedules(
        request,
        user_id=current_user["id"],
        user_uuid=user_uuid,
        db=db,
        page=page,
        items_per_page=items_per_page,
        gear_item_id=gear_item_id,
    )


@cache(key_prefix="user_{user_id}_gear_service_schedule", resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)
async def _cached_read_schedule(
    request: Request,
    user_id: int,
    uuid: uuid_pkg.UUID,
    owner_uuid: uuid_pkg.UUID,
    gear_item_uuid: uuid_pkg.UUID,
    db: AsyncSession,
) -> GearServiceScheduleRead:
    """Fetches (and caches) a single schedule. Authorization happens in the route - see
    `_cached_read_schedules`."""
    db_schedule = await crud_gear_service_schedules.get(
        db=db, uuid=uuid, schema_to_select=GearServiceScheduleReadInternal, return_as_model=True
    )
    if db_schedule is None:
        raise NotFoundException("Service schedule not found")

    return _to_public_schedule(
        cast(GearServiceScheduleReadInternal, db_schedule), user_uuid=owner_uuid, gear_item_uuid=gear_item_uuid
    )


@router.get("/gear-service-schedule/{uuid}", response_model=GearServiceScheduleRead)
async def read_gear_service_schedule(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> GearServiceScheduleRead:
    """Return a single service schedule by its public uuid.

    Ownership is inherited from the gear item the schedule hangs off, so a schedule on
    another user's item reads as 404 rather than 403 - its existence isn't disclosed.
    """
    schedule = await resolve_schedule_for_user(db=db, schedule_uuid=uuid, user_id=current_user["id"])
    if schedule is None:
        raise NotFoundException("Service schedule not found")

    uuid_by_id = await get_gear_item_uuids_by_id(db=db, gear_item_ids=[schedule.gear_item_id])
    # A miss means the item was deleted between the two statements, which took this schedule
    # with it - so the addressed resource is genuinely gone, and 404 is what the resolve
    # above would have answered had the delete landed a moment earlier.
    gear_item_uuid = uuid_by_id.get(schedule.gear_item_id)
    if gear_item_uuid is None:
        raise NotFoundException("Service schedule not found")

    return await _cached_read_schedule(
        request,
        user_id=current_user["id"],
        uuid=uuid,
        owner_uuid=current_user["uuid"],
        gear_item_uuid=gear_item_uuid,
        db=db,
    )


@router.patch("/gear-service-schedule/{uuid}")
async def patch_gear_service_schedule(
    request: Request,
    uuid: uuid_pkg.UUID,
    values: GearServiceScheduleUpdate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Partial update.

    The at-least-one-interval invariant can't live on `GearServiceScheduleUpdate` - a
    patch that sets only `interval_dives` says nothing about `interval_months` - so it's
    checked here against the merged result instead.
    """
    schedule = await resolve_schedule_for_user(db=db, schedule_uuid=uuid, user_id=current_user["id"])
    if schedule is None:
        raise NotFoundException("Service schedule not found")

    update_data = values.model_dump(exclude_unset=True)
    if not update_data:
        return {"message": "Service schedule updated"}

    effective_months = update_data.get("interval_months", schedule.interval_months)
    effective_dives = update_data.get("interval_dives", schedule.interval_dives)
    if effective_months is None and effective_dives is None:
        raise UnprocessableEntityException("A service schedule needs an interval in months, in dives, or both")

    effective_kind = update_data.get("kind", schedule.kind)
    effective_label = update_data.get("label", schedule.label)
    if ("kind" in update_data or "label" in update_data) and await schedule_kind_exists(
        db=db,
        gear_item_id=schedule.gear_item_id,
        kind=effective_kind if isinstance(effective_kind, str) else effective_kind.value,
        label=effective_label,
        exclude_id=schedule.id,
    ):
        raise DuplicateValueException("This gear item already has a schedule for that kind of service")

    await crud_gear_service_schedules.update(db=db, object=update_data, uuid=uuid)
    # `starts_on` and either interval move the due date, and `kind`/`label` change which
    # records count towards it - cheaper to always recalculate than to work out which
    # edits could have mattered.
    await recalculate_service_schedule(db=db, schedule_id=schedule.id)
    await invalidate_gear_caches(schedule.user_id)

    return {"message": "Service schedule updated"}


@router.delete("/gear-service-schedule/{uuid}")
async def erase_gear_service_schedule(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Delete a servicing rule.

    Service records that satisfied it stay in the item's history with their
    `gear_service_schedule_id` nulled - the FK's `ON DELETE SET NULL` finally fires, which
    is exactly what it was declared for: deleting a reminder must never throw away the
    receipts. 404 unless the caller owns it, and a second `DELETE` on the same uuid is a
    404 too.
    """
    schedule = await resolve_schedule_for_user(db=db, schedule_uuid=uuid, user_id=current_user["id"])
    if schedule is None:
        raise NotFoundException("Service schedule not found")

    await crud_gear_service_schedules.delete(db=db, uuid=uuid)
    await invalidate_gear_caches(schedule.user_id)

    return {"message": "Service schedule deleted"}


# -------------------- records --------------------
@router.post("/gear-service-record", response_model=GearServiceRecordRead, status_code=201)
async def write_gear_service_record(
    request: Request,
    record: GearServiceRecordCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> GearServiceRecordRead:
    """Log a servicing event.

    When `gear_service_schedule_uuid` is omitted the matching rule is inferred from
    (item, kind, label) - see `find_schedule_for_record` - so logging "annual service
    done" from the gear page satisfies the reminder without picking anything. A record
    with no matching rule is fine and simply stands on its own.
    """
    db_gear_item = await _owned_gear_item(db, record.gear_item_uuid, current_user["id"])

    schedule = None
    if record.gear_service_schedule_uuid is not None:
        schedule = await resolve_schedule_for_user(
            db=db, schedule_uuid=record.gear_service_schedule_uuid, user_id=current_user["id"]
        )
        if schedule is None or schedule.gear_item_id != db_gear_item.id:
            raise UnprocessableEntityException("Service schedule not found.")
    else:
        schedule = await find_schedule_for_record(
            db=db, gear_item_id=db_gear_item.id, kind=record.kind.value, label=record.label
        )

    record_internal = GearServiceRecordCreateInternal(
        **record.model_dump(exclude={"gear_item_uuid", "gear_service_schedule_uuid"}),
        user_id=current_user["id"],
        gear_item_id=db_gear_item.id,
        gear_service_schedule_id=schedule.id if schedule is not None else None,
        # Snapshot of the item's lifetime dive count at the moment of service - the
        # baseline the next dive-based threshold is measured from. Never client-supplied.
        dive_count_at_service=db_gear_item.dive_count,
    )
    created = await crud_gear_service_records.create(
        db=db, object=record_internal, schema_to_select=GearServiceRecordReadInternal, return_as_model=True
    )
    if schedule is not None:
        # Moves the due date onto this service and clears the notify state, so the
        # reminder re-arms for the next cycle.
        await recalculate_service_schedule(db=db, schedule_id=schedule.id)
    await invalidate_gear_caches(current_user["id"])

    record_read = await crud_gear_service_records.get(
        db=db, id=created.id, schema_to_select=GearServiceRecordReadInternal, return_as_model=True
    )
    if record_read is None:
        raise NotFoundException("Created service record not found")

    return _to_public_record(
        cast(GearServiceRecordReadInternal, record_read),
        user_uuid=current_user["uuid"],
        gear_item_uuid=db_gear_item.uuid,
        gear_service_schedule_uuid=schedule.uuid if schedule is not None else None,
    )


@cache(
    key_prefix=(
        "user_{user_id}_gear_service_records:page_{page}:items_per_page:{items_per_page}"
        ":item_{gear_item_id}:schedule_{gear_service_schedule_id}"
    ),
    resource_id_name="user_id",
    expiration=60,
)
async def _cached_read_records(
    request: Request,
    user_id: int,
    user_uuid: uuid_pkg.UUID,
    db: AsyncSession,
    page: int,
    items_per_page: int,
    gear_item_id: int | None,
    gear_service_schedule_id: int | None,
) -> dict:
    """Fetches (and caches) a user's paginated service history, newest work first.
    Authorization happens in the route - see `_cached_read_schedules`."""
    filters: dict[str, Any] = {"user_id": user_id, "is_deleted": False}
    if gear_item_id is not None:
        filters["gear_item_id"] = gear_item_id
    if gear_service_schedule_id is not None:
        filters["gear_service_schedule_id"] = gear_service_schedule_id

    data = await crud_gear_service_records.get_multi(
        db=db,
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
        sort_columns=["serviced_on", "id"],
        sort_orders=["desc", "desc"],
        **filters,
    )
    uuid_by_id = await get_gear_item_uuids_by_id(db=db, gear_item_ids=[row["gear_item_id"] for row in data["data"]])
    schedule_uuid_by_id = await _schedule_uuids_by_id(
        db=db, schedule_ids=[row["gear_service_schedule_id"] for row in data["data"]]
    )
    # `.get()`-and-skip on the item, for the reason `_cached_read_schedules` gives; the
    # schedule half was always a `.get()` because that reference is legitimately nullable.
    data["data"] = [
        _to_public_record(
            row,
            user_uuid=user_uuid,
            gear_item_uuid=item_uuid,
            gear_service_schedule_uuid=schedule_uuid_by_id.get(row["gear_service_schedule_id"]),
        ).model_dump()
        for row in data["data"]
        if (item_uuid := uuid_by_id.get(row["gear_item_id"])) is not None
    ]

    response: dict[str, Any] = paginated_response(crud_data=data, page=page, items_per_page=items_per_page)
    return response


async def _schedule_uuids_by_id(db: AsyncSession, schedule_ids: list[int | None]) -> dict[int | None, uuid_pkg.UUID]:
    """Resolve *live* schedule ids to public uuids for a page of records, in one query.

    Both the parameter and the key type are `int | None` because a record's
    `gear_service_schedule_id` is nullable: the Nones are dropped here rather than at
    every call site, and the widened key lets callers `.get()` a nullable id directly
    (which correctly yields `None`, since the returned mapping never has a `None` key).

    The `.get()` at both call sites stays load-bearing: a record whose schedule was
    deleted has its `gear_service_schedule_id` nulled by the FK's `ON DELETE SET NULL`, so
    the id is dropped by the `if schedule_id is not None` above and the mapping legitimately
    has no entry for it. `gear_service_schedule_uuid` is `UUID | None` and documented as
    null when the rule is gone, which is exactly the state that produces.

    No liveness filter, and none is reachable - a schedule row cannot outlive itself. It
    carried one through the soft-delete era for the same reason the null exists; see "The
    service-record resolvers split, and only one of them was the same question" in
    DECISIONS.md.
    """
    wanted = {schedule_id for schedule_id in schedule_ids if schedule_id is not None}
    if not wanted:
        return {}

    rows = await crud_gear_service_schedules.get_multi(
        db=db,
        id__in=list(wanted),
        limit=len(wanted),
        schema_to_select=GearServiceScheduleReadInternal,
    )
    return {row["id"]: row["uuid"] for row in rows["data"]}


@router.get("/gear-service-records", response_model=PaginatedListResponse[GearServiceRecordRead])
async def read_gear_service_records(
    request: Request,
    user_uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    gear_item_uuid: uuid_pkg.UUID | None = None,
    gear_service_schedule_uuid: uuid_pkg.UUID | None = None,
    page: int = 1,
    items_per_page: int = 10,
) -> dict:
    """List a user's service history, optionally narrowed to one gear item or schedule."""
    if current_user["uuid"] != user_uuid:
        raise ForbiddenException()

    page, items_per_page = clamp_pagination(page, items_per_page)

    gear_item_id = None
    if gear_item_uuid is not None:
        gear_item_id = (await _owned_gear_item(db, gear_item_uuid, current_user["id"])).id

    schedule_id = None
    if gear_service_schedule_uuid is not None:
        schedule = await resolve_schedule_for_user(
            db=db, schedule_uuid=gear_service_schedule_uuid, user_id=current_user["id"]
        )
        if schedule is None:
            raise UnprocessableEntityException("Service schedule not found.")
        schedule_id = schedule.id

    return await _cached_read_records(
        request,
        user_id=current_user["id"],
        user_uuid=user_uuid,
        db=db,
        page=page,
        items_per_page=items_per_page,
        gear_item_id=gear_item_id,
        gear_service_schedule_id=schedule_id,
    )


@cache(key_prefix="user_{user_id}_gear_service_record", resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)
async def _cached_read_record(
    request: Request,
    user_id: int,
    uuid: uuid_pkg.UUID,
    owner_uuid: uuid_pkg.UUID,
    gear_item_uuid: uuid_pkg.UUID,
    gear_service_schedule_uuid: uuid_pkg.UUID | None,
    db: AsyncSession,
) -> GearServiceRecordRead:
    """Fetches (and caches) a single service record. Authorization happens in the route."""
    db_record = await crud_gear_service_records.get(
        db=db, uuid=uuid, is_deleted=False, schema_to_select=GearServiceRecordReadInternal, return_as_model=True
    )
    if db_record is None:
        raise NotFoundException("Service record not found")

    return _to_public_record(
        cast(GearServiceRecordReadInternal, db_record),
        user_uuid=owner_uuid,
        gear_item_uuid=gear_item_uuid,
        gear_service_schedule_uuid=gear_service_schedule_uuid,
    )


@router.get("/gear-service-record/{uuid}", response_model=GearServiceRecordRead)
async def read_gear_service_record(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> GearServiceRecordRead:
    """Return a single service record by its public uuid.

    Ownership is inherited from the gear item the record belongs to, so another user's
    record reads as 404 rather than 403 - its existence isn't disclosed.
    """
    record = await resolve_record_for_user(db=db, record_uuid=uuid, user_id=current_user["id"])
    if record is None:
        raise NotFoundException("Service record not found")

    uuid_by_id = await get_gear_item_uuids_by_id(db=db, gear_item_ids=[record.gear_item_id])
    schedule_uuid_by_id = await _schedule_uuids_by_id(db=db, schedule_ids=[record.gear_service_schedule_id])
    # Same race as on the schedule half: the item going takes this record with it.
    gear_item_uuid = uuid_by_id.get(record.gear_item_id)
    if gear_item_uuid is None:
        raise NotFoundException("Service record not found")

    return await _cached_read_record(
        request,
        user_id=current_user["id"],
        uuid=uuid,
        owner_uuid=current_user["uuid"],
        gear_item_uuid=gear_item_uuid,
        gear_service_schedule_uuid=schedule_uuid_by_id.get(record.gear_service_schedule_id),
        db=db,
    )


@router.patch("/gear-service-record/{uuid}")
async def patch_gear_service_record(
    request: Request,
    uuid: uuid_pkg.UUID,
    values: GearServiceRecordUpdate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Partial update. Correcting a `serviced_on` date moves the schedule's next due
    date, so the linked rule is recalculated afterwards.

    `dive_count_at_service` isn't editable - it records a moment that has already
    passed, and rewriting it would silently shift a dive-based threshold.
    """
    record = await resolve_record_for_user(db=db, record_uuid=uuid, user_id=current_user["id"])
    if record is None:
        raise NotFoundException("Service record not found")

    update_data = values.model_dump(exclude_unset=True)
    if not update_data:
        return {"message": "Service record updated"}

    await crud_gear_service_records.update(db=db, object=update_data, uuid=uuid)
    if record.gear_service_schedule_id is not None:
        await recalculate_service_schedule(db=db, schedule_id=record.gear_service_schedule_id)
    await invalidate_gear_caches(record.user_id)

    return {"message": "Service record updated"}


@router.delete("/gear-service-record/{uuid}")
async def erase_gear_service_record(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Soft-deletes a service record. The schedule falls back to the previous record (or
    to its own `starts_on` if that was the only one), which is exactly what deleting a
    mistakenly-logged service should do.
    """
    record = await resolve_record_for_user(db=db, record_uuid=uuid, user_id=current_user["id"])
    if record is None:
        raise NotFoundException("Service record not found")

    await crud_gear_service_records.delete(db=db, uuid=uuid)
    if record.gear_service_schedule_id is not None:
        await recalculate_service_schedule(db=db, schedule_id=record.gear_service_schedule_id)
    await invalidate_gear_caches(record.user_id)

    return {"message": "Service record deleted"}


# -------------------- dashboard --------------------
@cache(key_prefix="user_{user_id}_gear_service_due", resource_id_name="user_id", expiration=60)
async def _cached_read_due(request: Request, user_id: int, db: AsyncSession) -> dict:
    """Fetches (and caches) the user's whole active schedule list. Authorization happens
    in the route - see `_cached_read_schedules`."""
    data, truncated = await get_due_overview_for_user(db=db, user_id=user_id, limit=DUE_OVERVIEW_LIMIT)
    return GearServiceDueResponse(data=data, truncated=truncated).model_dump()


@router.get("/gear-service-due", response_model=GearServiceDueResponse)
async def read_gear_service_due(
    request: Request,
    user_uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict:
    """Every active schedule the user owns, with enough of each gear item to render a
    dashboard line.

    Takes no date horizon on purpose: filtering by "due within N days" server-side would
    bake today's date into the cached response, which then quietly goes wrong at
    midnight. The client buckets into due-soon/overdue itself.
    """
    if current_user["uuid"] != user_uuid:
        raise ForbiddenException()

    return await _cached_read_due(request, user_id=current_user["id"], db=db)
