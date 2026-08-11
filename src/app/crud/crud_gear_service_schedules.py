import uuid as uuid_pkg
from typing import Any

from fastcrud import FastCRUD
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.gear_item import GearItem
from ..models.gear_service_schedule import GearServiceSchedule
from ..schemas.gear_service import (
    GearServiceDueItem,
    GearServiceScheduleCreateInternal,
    GearServiceScheduleDelete,
    GearServiceScheduleInfo,
    GearServiceScheduleReadInternal,
    GearServiceScheduleUpdate,
    GearServiceScheduleUpdateInternal,
)

# The `gear_service_schedule` columns making up a `GearServiceScheduleInfo` (the summary
# shape embedded in a `GearItemRead`), in the order `service_schedule_info_from_row`
# unpacks them. Mirrors `GEAR_ITEM_INFO_COLUMNS` in `crud_gear_items`.
#
# Deliberately no status field: status depends on today's date and would go stale inside
# a cached response, so only clock-stable facts are carried (see `ServiceStatus`).
SERVICE_SCHEDULE_INFO_COLUMNS = (
    GearServiceSchedule.uuid,
    GearServiceSchedule.kind,
    GearServiceSchedule.label,
    GearServiceSchedule.interval_months,
    GearServiceSchedule.interval_dives,
    GearServiceSchedule.last_service_on,
    GearServiceSchedule.next_due_on,
    GearServiceSchedule.next_due_at_dive_count,
    GearServiceSchedule.is_active,
)


def service_schedule_info_from_row(row: Any) -> GearServiceScheduleInfo:
    """Build a `GearServiceScheduleInfo` from a row selecting `SERVICE_SCHEDULE_INFO_COLUMNS`."""
    return GearServiceScheduleInfo(
        uuid=row.uuid,
        kind=row.kind,
        label=row.label,
        interval_months=row.interval_months,
        interval_dives=row.interval_dives,
        last_service_on=row.last_service_on,
        next_due_on=row.next_due_on,
        next_due_at_dive_count=row.next_due_at_dive_count,
        is_active=row.is_active,
    )


CRUDGearServiceSchedule = FastCRUD[
    GearServiceSchedule,
    GearServiceScheduleCreateInternal,
    GearServiceScheduleUpdate,
    GearServiceScheduleUpdateInternal,
    GearServiceScheduleDelete,
    GearServiceScheduleReadInternal,
]
crud_gear_service_schedules = CRUDGearServiceSchedule(GearServiceSchedule)


# Schedules are ordered by how soon they need attention, with rules that have no due
# date at all (dive-only intervals) last. Shared by the single and batched fetches so
# both render in the same order.
_INFO_ORDER = (GearServiceSchedule.next_due_on.asc().nulls_last(), GearServiceSchedule.kind)


async def get_schedules_for_gear_item(db: AsyncSession, gear_item_id: int) -> list[GearServiceScheduleInfo]:
    """Return a gear item's service schedules, soonest-due first."""
    result = await db.execute(
        select(*SERVICE_SCHEDULE_INFO_COLUMNS)
        .where(
            GearServiceSchedule.gear_item_id == gear_item_id,
            GearServiceSchedule.is_deleted.is_(False),
        )
        .order_by(*_INFO_ORDER)
    )
    return [service_schedule_info_from_row(row) for row in result]


async def get_schedules_for_gear_items(
    db: AsyncSession, gear_item_ids: list[int]
) -> dict[int, list[GearServiceScheduleInfo]]:
    """Batched version of `get_schedules_for_gear_item`, for the paginated gear listing.

    Without this the gear list would run one query per row just to show a "service due"
    badge. Mirrors `get_gear_items_for_dives`; served by
    `ux_gear_service_schedule_item_kind_label`, whose leading column is `gear_item_id`.
    """
    schedules_by_item: dict[int, list[GearServiceScheduleInfo]] = {item_id: [] for item_id in gear_item_ids}
    if not gear_item_ids:
        return schedules_by_item

    result = await db.execute(
        select(GearServiceSchedule.gear_item_id, *SERVICE_SCHEDULE_INFO_COLUMNS)
        .where(
            GearServiceSchedule.gear_item_id.in_(gear_item_ids),
            GearServiceSchedule.is_deleted.is_(False),
        )
        .order_by(GearServiceSchedule.gear_item_id, *_INFO_ORDER)
    )
    for row in result:
        schedules_by_item[row.gear_item_id].append(service_schedule_info_from_row(row))
    return schedules_by_item


async def schedule_kind_exists(
    db: AsyncSession, gear_item_id: int, kind: str, label: str | None = None, exclude_id: int | None = None
) -> bool:
    """Case-insensitive check for whether a non-deleted schedule of the same (kind, label)
    already exists on the item.

    Mirrors the `ux_gear_service_schedule_item_kind_label` partial unique index, so the
    route can return a friendly 422 instead of a raw integrity error - the same
    application-level/DB-level pairing as `gear_item_name_exists`. A NULL label and an
    empty one are treated as the same "unlabelled" slot, matching the index's COALESCE.
    """
    normalized = (label or "").strip().lower()
    stmt = select(GearServiceSchedule.id).where(
        GearServiceSchedule.gear_item_id == gear_item_id,
        GearServiceSchedule.is_deleted.is_(False),
        GearServiceSchedule.kind == kind,
        func.coalesce(func.lower(GearServiceSchedule.label), "") == normalized,
    )
    if exclude_id is not None:
        stmt = stmt.where(GearServiceSchedule.id != exclude_id)

    result = await db.execute(stmt.limit(1))
    return result.first() is not None


async def get_due_overview_for_user(
    db: AsyncSession, user_id: int, limit: int
) -> tuple[list[GearServiceDueItem], bool]:
    """Every active schedule a user owns, joined to enough of its gear item to render a
    dashboard line, soonest-due first. Returns `(rows, truncated)`.

    Deliberately unfiltered by any date horizon: baking "today" into the query would
    bake it into the cached response too, which then goes quietly wrong at midnight. The
    caller buckets into due-soon/overdue itself (see `GearServiceDueResponse`).

    Archived gear is excluded, matching the digest job: retiring a piece of kit should
    stop it asking for attention without the diver having to also pause every rule on it.

    Selects one row past `limit` so `truncated` is exact rather than the "we returned
    exactly `limit` rows, so there are *probably* more" guess that comparing lengths
    would give. The extra row is dropped before returning.
    """
    result = await db.execute(
        select(
            GearServiceSchedule.uuid.label("schedule_uuid"),
            GearServiceSchedule.kind,
            GearServiceSchedule.label,
            GearServiceSchedule.last_service_on,
            GearServiceSchedule.next_due_on,
            GearServiceSchedule.next_due_at_dive_count,
            GearItem.uuid.label("gear_item_uuid"),
            GearItem.name.label("gear_item_name"),
            GearItem.brand.label("gear_item_brand"),
            GearItem.dive_count.label("gear_item_dive_count"),
        )
        .join(GearItem, GearItem.id == GearServiceSchedule.gear_item_id)
        .where(
            GearServiceSchedule.user_id == user_id,
            GearServiceSchedule.is_deleted.is_(False),
            GearServiceSchedule.is_active.is_(True),
            GearItem.is_deleted.is_(False),
            GearItem.is_archived.is_(False),
        )
        .order_by(GearServiceSchedule.next_due_on.asc().nulls_last(), GearItem.name)
        .limit(limit + 1)
    )
    rows = [GearServiceDueItem.model_validate(row, from_attributes=True) for row in result]
    return rows[:limit], len(rows) > limit


async def resolve_schedule_for_user(
    db: AsyncSession, schedule_uuid: uuid_pkg.UUID, user_id: int
) -> GearServiceSchedule | None:
    """Resolve a schedule's public `uuid` to its row, scoped to non-deleted schedules
    owned by the given user.

    Returns `None` when the uuid doesn't exist or belongs to somebody else - callers
    turn both into a 404, so an outsider can't tell a real schedule from an imaginary one.
    """
    result = await db.execute(
        select(GearServiceSchedule).where(
            GearServiceSchedule.uuid == schedule_uuid,
            GearServiceSchedule.user_id == user_id,
            GearServiceSchedule.is_deleted.is_(False),
        )
    )
    return result.scalar_one_or_none()
