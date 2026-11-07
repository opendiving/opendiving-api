"""A dive as the detail route reads it, for whatever else needs that shape.

`GET /dive/{uuid}` caches what `load_dive_detail` answers; the import reads it uncached, inside
its own transaction, since a cache hit there would answer for a logbook the import has moved.
"""

import uuid as uuid_pkg
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.exceptions.http_exceptions import NotFoundException
from ..crud.crud_contacts import get_contact_uuids_by_ids
from ..crud.crud_courses import get_course_uuids_by_ids
from ..crud.crud_dive_dive_sites import get_dive_sites_for_dive
from ..crud.crud_dive_gear_items import get_gear_items_for_dive
from ..crud.crud_dive_mixtures import get_mixtures_for_dive
from ..crud.crud_dive_species import get_species_for_dive
from ..crud.crud_dives import crud_dives
from ..crud.crud_people import get_people_for_dives
from ..crud.crud_tags import get_tags_for_dives
from ..crud.crud_trips import get_trip_uuids_by_ids
from ..schemas.dive import DiveReadInternal, DiveReadWithMixtures, DiveSiteInfo, RecordingRead, SightingRead
from ..schemas.dive_conditions import DiveConditionsRead
from ..schemas.dive_mixture import DiveMixtureRead
from ..schemas.gear_item import GearItemInfo
from ..schemas.person import PersonReferenceRead
from .dive_conditions import get_conditions
from .dive_gas import resolve_gas_use
from .dive_list_items import INTERNAL_KEYS, to_public_start_time
from .dive_profiles import ProfileGasAttribution, get_gas_attribution_for_dives
from .dive_recordings import get_recordings_for_dives


def to_public_dive_with_mixtures(
    db_dive: DiveReadInternal | dict[str, Any],
    *,
    user_uuid: uuid_pkg.UUID,
    trip_uuid: uuid_pkg.UUID | None,
    course_uuid: uuid_pkg.UUID | None,
    contact_uuid: uuid_pkg.UUID | None,
    dive_sites: list[DiveSiteInfo],
    gear_items: list[GearItemInfo],
    mixtures: list[DiveMixtureRead],
    sightings: list[SightingRead],
    people: list[PersonReferenceRead],
    tags: list[str],
    recordings: list[RecordingRead] | None = None,
    attribution: ProfileGasAttribution | None = None,
    conditions: DiveConditionsRead | None = None,
) -> DiveReadWithMixtures:
    """Assemble a dive's public shape from the row plus everything a read embeds.

    Internal FKs are dropped in favour of the related resources' uuids and summaries, so
    the related rows are passed in already fetched - the caller batches them across a page
    rather than querying per dive.

    `attribution` is the one input here that never reaches the response as itself: it is
    the primary recording's account of which cylinder was breathed when, and it exists
    solely so a multi-cylinder dive can produce `gas_use`. `None` on the create path, where
    the dive cannot yet have a recording to have been extracted from - which is also why
    `recordings` defaults to empty there rather than being queried for.
    """
    data = to_public_start_time(db_dive if isinstance(db_dive, dict) else db_dive.model_dump())
    return DiveReadWithMixtures(
        **{k: v for k, v in data.items() if k not in INTERNAL_KEYS},
        user_uuid=user_uuid,
        trip_uuid=trip_uuid,
        course_uuid=course_uuid,
        contact_uuid=contact_uuid,
        dive_sites=dive_sites,
        gear_items=gear_items,
        mixtures=mixtures,
        sightings=sightings,
        people=people,
        tags=tags,
        recordings=recordings or [],
        # Both callers of this function (creating a dive, and the cached single-dive
        # read) go through here, so gas use is derived in exactly one place. Safe to
        # compute before caching, unlike gear service status: nothing about it depends
        # on when it's read.
        gas_use=resolve_gas_use(
            submerged_time=data["submerged_time"],
            avg_depth=data["avg_depth"],
            mixtures=mixtures,
            attribution=attribution,
        ),
        conditions=conditions,
    )


async def load_dive_detail(
    db: AsyncSession, *, user_id: int, owner_uuid: uuid_pkg.UUID, uuid: uuid_pkg.UUID
) -> DiveReadWithMixtures:
    """One live dive with everything a read embeds, or a 404. Checks no ownership: the caller
    has settled that, and `user_id` is the dive's owner."""
    db_dive = await crud_dives.get(db=db, uuid=uuid, is_deleted=False, schema_to_select=DiveReadInternal)
    if db_dive is None:
        raise NotFoundException("Dive not found")
    db_dive = cast(dict[str, Any], db_dive)

    trip_uuid: uuid_pkg.UUID | None = None
    if db_dive["trip_id"] is not None:
        trip_uuid_by_id = await get_trip_uuids_by_ids(db=db, trip_ids=[db_dive["trip_id"]], user_id=user_id)
        trip_uuid = trip_uuid_by_id.get(db_dive["trip_id"])

    course_uuid: uuid_pkg.UUID | None = None
    if db_dive["course_id"] is not None:
        course_uuid_by_id = await get_course_uuids_by_ids(db=db, course_ids=[db_dive["course_id"]], user_id=user_id)
        course_uuid = course_uuid_by_id.get(db_dive["course_id"])

    contact_uuid = (await get_contact_uuids_by_ids(db=db, contact_ids=[db_dive["contact_id"]], user_id=user_id)).get(
        db_dive["contact_id"]
    )

    mixtures = await get_mixtures_for_dive(db=db, dive_id=db_dive["id"])
    dive_sites = await get_dive_sites_for_dive(db=db, dive_id=db_dive["id"])
    gear_items = await get_gear_items_for_dive(db=db, dive_id=db_dive["id"])
    sightings = await get_species_for_dive(db=db, dive_id=db_dive["id"])
    people = (await get_people_for_dives(db, [db_dive["id"]]))[db_dive["id"]]
    tags = (await get_tags_for_dives(db, [db_dive["id"]]))[db_dive["id"]]
    # Written batched though only ever called with one id - see `get_recordings_for_dives`.
    # This is three queries rather than the two `source_file` and `profile` used to cost, and
    # the third is what a dive with two computers needs: its recordings, their files and
    # their profile summaries cannot be read as one row any more.
    recordings = (await get_recordings_for_dives(db=db, dive_ids=[db_dive["id"]])).get(db_dive["id"], [])
    # A second narrow read of the same row, rather than a column on the summary above: this
    # one is never serialized, and it is the query `gas_use_history` needs on its own over
    # a whole log. See `get_gas_attribution_for_dives`.
    #
    # Only for a dive that can use it, which the mixtures just read already say. Anything
    # else - every single-cylinder dive, and every dive logged without a cylinder at all -
    # would be paying a round trip against the row the recordings read just touched, for a
    # value `compute_multi_tank_gas_use` discards on its first line.
    attribution = None
    if len(mixtures) >= 2:
        attribution = (await get_gas_attribution_for_dives(db=db, dive_ids=[db_dive["id"]]))[db_dive["id"]]
    conditions = await get_conditions(db, db_dive["id"])

    return to_public_dive_with_mixtures(
        db_dive,
        user_uuid=owner_uuid,
        trip_uuid=trip_uuid,
        course_uuid=course_uuid,
        contact_uuid=contact_uuid,
        dive_sites=dive_sites,
        gear_items=gear_items,
        mixtures=mixtures,
        sightings=sightings,
        people=people,
        tags=tags,
        recordings=recordings,
        attribution=attribution,
        conditions=conditions,
    )
