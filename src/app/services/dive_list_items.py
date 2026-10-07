"""A page of dive rows as the public `DiveListItem`s a dive list serves.

Two routes list dives - `GET /dives` and `GET /trip/{uuid}/dives` - and both hand back the
same row shape, so the enrichment lives here rather than in either route module, neither of
which may import the other.
"""

import uuid as uuid_pkg
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.utils.datetime_offset import combine_dive_start_time
from ..crud.crud_contacts import get_contact_uuids_by_ids
from ..crud.crud_courses import get_course_uuids_by_ids
from ..crud.crud_dive_dive_sites import get_dive_sites_for_dives
from ..crud.crud_dive_gear_items import get_gear_items_for_dives
from ..crud.crud_trips import get_trip_uuids_by_ids
from ..schemas.dive import DiveListItem, DiveReadInternal, DiveSiteInfo
from ..schemas.dive_profile import DepthOutline
from ..schemas.gear_item import GearItemInfo
from .dive_profiles import get_depth_outlines_for_dives
from .dive_recordings import recording_counts_for_dives

# The internal keys a public dive drops in favour of the uuids it resolves them to.
INTERNAL_KEYS = frozenset({"id", "user_id", "trip_id", "course_id", "contact_id"})


def to_public_start_time(data: dict[str, Any]) -> dict[str, Any]:
    """Re-attaches a stored `utc_offset_minutes` to `start_time` and drops the now-redundant
    offset and date-only keys, so the public `DiveRead`/`DiveReadWithMixtures` shape exposes a
    single `start_time` (e.g. `2021-04-04T10:04:47.910+02:00`) rather than a column triple -
    see `core/utils/datetime_offset.py`.

    **Offset-aware for every dive but two kinds.** A dive whose source recorded no offset
    stores a NULL there, and the recorded wall clock comes back with no zone attached
    (`2021-04-04T10:04:47.910`); one whose source recorded no time of day comes back as its
    bare date (`2021-04-04`). Only logbook import can create either; the read shapes carry
    `DiveLocalStartTime` so they can serve both, `DiveCreate` still requires an offset, and
    `patch_dive` keeps each state only for a dive already in it.
    """
    data = dict(data)
    offset_minutes = data.pop("utc_offset_minutes")
    date_only = data.pop("start_date_only", False)
    data["start_time"] = combine_dive_start_time(data["start_time"], offset_minutes, date_only)
    return data


def to_public_dive(
    db_dive: DiveReadInternal | dict[str, Any],
    *,
    user_uuid: uuid_pkg.UUID,
    trip_uuid: uuid_pkg.UUID | None,
    course_uuid: uuid_pkg.UUID | None,
    contact_uuid: uuid_pkg.UUID | None,
    dive_sites: list[DiveSiteInfo],
    gear_items: list[GearItemInfo],
    depth_outline: DepthOutline | None,
    recording_count: int,
) -> DiveListItem:
    """Convert an internal dive representation (integer FKs) into its public list-row shape
    (owning user, trip, training course and contact referenced by `uuid`)."""
    data = to_public_start_time(db_dive if isinstance(db_dive, dict) else db_dive.model_dump())
    return DiveListItem(
        **{k: v for k, v in data.items() if k not in INTERNAL_KEYS},
        user_uuid=user_uuid,
        trip_uuid=trip_uuid,
        course_uuid=course_uuid,
        contact_uuid=contact_uuid,
        dive_sites=dive_sites,
        gear_items=gear_items,
        depth_outline=depth_outline,
        recording_count=recording_count,
    )


async def to_dive_list_items(
    db: AsyncSession, rows: list[dict[str, Any]], *, user_id: int, user_uuid: uuid_pkg.UUID
) -> list[dict[str, Any]]:
    """Each row - every `dive` column, as `get_dives_page` returns it - with its dive site(s),
    gear, trip/course/contact uuids, depth outline and recording count, through one batched lookup apiece for
    the whole page."""
    dive_ids = [d["id"] for d in rows]
    sites_by_dive = await get_dive_sites_for_dives(db=db, dive_ids=dive_ids)
    gear_by_dive = await get_gear_items_for_dives(db=db, dive_ids=dive_ids)
    referenced_trip_ids = [d["trip_id"] for d in rows if d["trip_id"] is not None]
    trip_uuid_by_id = await get_trip_uuids_by_ids(db=db, trip_ids=referenced_trip_ids, user_id=user_id)
    referenced_course_ids = [d["course_id"] for d in rows if d["course_id"] is not None]
    course_uuid_by_id = await get_course_uuids_by_ids(db=db, course_ids=referenced_course_ids, user_id=user_id)
    contact_uuid_by_id = await get_contact_uuids_by_ids(
        db=db, contact_ids=[d["contact_id"] for d in rows], user_id=user_id
    )
    outline_by_dive = await get_depth_outlines_for_dives(db, dive_ids=dive_ids)
    recording_count_by_dive = await recording_counts_for_dives(db, dive_ids=dive_ids)

    return [
        to_public_dive(
            dive,
            user_uuid=user_uuid,
            trip_uuid=trip_uuid_by_id.get(dive["trip_id"]) if dive["trip_id"] is not None else None,
            course_uuid=course_uuid_by_id.get(dive["course_id"]) if dive["course_id"] is not None else None,
            contact_uuid=contact_uuid_by_id.get(dive["contact_id"]),
            dive_sites=sites_by_dive.get(dive["id"], []),
            gear_items=gear_by_dive.get(dive["id"], []),
            depth_outline=outline_by_dive.get(dive["id"]),
            recording_count=recording_count_by_dive.get(dive["id"], 0),
        ).model_dump()
        for dive in rows
    ]
