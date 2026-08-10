import uuid as uuid_pkg

from fastcrud import FastCRUD
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.gear_service_record import GearServiceRecord
from ..models.gear_service_schedule import GearServiceSchedule
from ..schemas.gear_service import (
    GearServiceRecordCreateInternal,
    GearServiceRecordDelete,
    GearServiceRecordReadInternal,
    GearServiceRecordUpdate,
    GearServiceRecordUpdateInternal,
)

CRUDGearServiceRecord = FastCRUD[
    GearServiceRecord,
    GearServiceRecordCreateInternal,
    GearServiceRecordUpdate,
    GearServiceRecordUpdateInternal,
    GearServiceRecordDelete,
    GearServiceRecordReadInternal,
]
crud_gear_service_records = CRUDGearServiceRecord(GearServiceRecord)


async def find_schedule_for_record(
    db: AsyncSession, gear_item_id: int, kind: str, label: str | None = None
) -> GearServiceSchedule | None:
    """Find the one schedule a service record should satisfy, inferred from its
    (item, kind, label).

    This is what lets "log the annual service" from the gear detail page satisfy the
    reminder without the diver picking a schedule from a dropdown. There can only ever
    be one match - `ux_gear_service_schedule_item_kind_label` enforces exactly that
    uniqueness - so inferring it is unambiguous rather than a guess.

    Returns `None` when the item has no matching rule, which is a perfectly ordinary
    outcome: logging "hydro done" on a cylinder with no reminder set up is a real thing
    to want to do, and the record stands on its own.
    """
    result = await db.execute(
        select(GearServiceSchedule).where(
            GearServiceSchedule.gear_item_id == gear_item_id,
            GearServiceSchedule.is_deleted.is_(False),
            GearServiceSchedule.kind == kind,
            func.coalesce(func.lower(GearServiceSchedule.label), "") == (label or "").strip().lower(),
        )
    )
    return result.scalar_one_or_none()


async def resolve_record_for_user(
    db: AsyncSession, record_uuid: uuid_pkg.UUID, user_id: int
) -> GearServiceRecord | None:
    """Resolve a record's public `uuid` to its row, scoped to non-deleted records owned
    by the given user. `None` covers both "doesn't exist" and "isn't yours" - see
    `resolve_schedule_for_user`.
    """
    result = await db.execute(
        select(GearServiceRecord).where(
            GearServiceRecord.uuid == record_uuid,
            GearServiceRecord.user_id == user_id,
            GearServiceRecord.is_deleted.is_(False),
        )
    )
    return result.scalar_one_or_none()
