import hashlib
import uuid as uuid_pkg
from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from ...api.dependencies import fetch_owned_or_raise, get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import (
    BadRequestException,
    NotFoundException,
    UnprocessableEntityException,
)
from ...core.security import create_dive_file_token
from ...core.utils.cache import cache
from ...core.utils.datetime_offset import (
    combine_dive_start_time,
    combine_start_time,
    split_local_start_time,
    split_start_time,
    split_updated_start_time,
)
from ...core.utils.pagination import clamp_pagination
from ...core.utils.uploads import content_disposition_attachment, read_upload_within_limit
from ...crud.crud_contacts import get_contact_refs_by_ids
from ...crud.crud_courses import get_course_uuids_by_ids, resolve_course_id_for_user
from ...crud.crud_dive_dive_sites import (
    get_dive_sites_for_dive,
    get_dive_sites_for_dives,
    replace_dive_sites_for_dive,
)
from ...crud.crud_dive_gear_items import (
    get_gear_items_for_dive,
    get_gear_items_for_dives,
    replace_gear_items_for_dive,
)
from ...crud.crud_dive_mixtures import get_mixtures_for_dive, replace_mixtures_for_dive
from ...crud.crud_dive_sites import resolve_dive_site_ids_for_user
from ...crud.crud_dive_species import (
    get_species_for_dive,
    replace_species_for_dive,
)
from ...crud.crud_dives import crud_dives
from ...crud.crud_gear_items import resolve_gear_item_ids_for_user
from ...crud.crud_species import resolve_species_ids
from ...crud.crud_trips import get_trip_uuids_by_ids, resolve_trip_id_for_user
from ...schemas.dive import (
    DiveCreateInternal,
    DiveCreateRequest,
    DiveMergeRequest,
    DiveMergeResult,
    DiveNeighbors,
    DiveNumberingSummary,
    DiveNumberSuggestion,
    DiveRead,
    DiveReadInternal,
    DiveReadWithMixtures,
    DiveRenumberRequest,
    DiveRenumberResult,
    DiveSiteInfo,
    DiveStartTime,
    DiveUpdateRequest,
    RecordingRead,
    RecordingUpdateRequest,
    SpeciesInfo,
    validate_depth_pair,
)
from ...schemas.dive_mixture import DiveMixtureRead
from ...schemas.dive_profile import RecordingProfileRead
from ...schemas.gear_item import GearItemInfo
from ...schemas.parsed_dive import ParsedDevice, ParsedDiveMatch, ParsedDiveResponse, ParsedDiveSchema
from ...services.cache_invalidation import invalidate_dive_caches, invalidate_gear_caches
from ...services.contact_links import CONTACT_NOT_FOUND, resolve_contact_reference
from ...services.dive_files import (
    MAX_DIVE_FILE_SIZE,
    DiveFileAlreadyLinkedError,
    DiveFileConflictError,
    DiveFileNotFoundError,
    InvalidDiveFileTokenError,
    delete_dive_file,
    delete_files_for_dive,
    get_dive_file_sha256,
    load_dive_file,
    refresh_tech_scalars,
    resolve_dive_file,
    store_recording_file,
)
from ...services.dive_gas import resolve_gas_use
from ...services.dive_merge import DiveNotMergeableError, merge_dives
from ...services.dive_neighbors import find_dive_neighbors
from ...services.dive_numbering import renumber_dives, suggest_dive_number, summarize_numbering
from ...services.dive_parsers import DiveParseError, UnsupportedDiveFileError, parse_dive_file_with_parser
from ...services.dive_profiles import (
    ProfileGasAttribution,
    get_gas_attribution_for_dives,
    get_profile_version,
    load_profile,
    to_recording_read_schema,
)
from ...services.dive_recordings import (
    DeviceIdentity,
    RecordingFacts,
    RecordingNotFoundError,
    delete_recording,
    device_of,
    get_recordings_for_dives,
    is_same_dive_loose,
    is_same_recording,
    load_candidates,
    make_primary,
    primary_recording_ids,
    resolve_recording,
    start_delta,
)
from ...services.dive_stats import recalculate_dive_stats
from ...services.gear_stats import recalculate_gear_dive_counts

router = APIRouter(tags=["dives"])


_DIVE_CONSTRAINT_MESSAGES = {
    "ck_dive_duration_positive": "Duration must be positive.",
    "ck_dive_visibility_non_negative": "Visibility must be zero or positive.",
    "ck_dive_max_depth_positive": "Max depth must be positive.",
    "ck_dive_avg_depth_positive": "Average depth must be positive.",
    "ck_dive_weight_non_negative": "Weight must be zero or positive.",
    "ck_dive_altitude_range": "Altitude must be between -450 and 6500 meters.",
    # Unreachable through the form - these columns are written only by the import path
    # (see DECISIONS.md) - but a constraint with no message here is worse than a raw 500:
    # both write paths already wrap `IntegrityError`, so an unmapped constraint falls
    # through to the generic "Invalid reference" below and 422s with a sentence about
    # something else entirely. A violation would mean a parser unit bug, so the messages
    # say so.
    "ck_dive_entry_latitude_range": "Imported latitudes must be between -90 and 90.",
    "ck_dive_exit_latitude_range": "Imported latitudes must be between -90 and 90.",
    "ck_dive_entry_longitude_range": "Imported longitudes must be between -180 and 180.",
    "ck_dive_exit_longitude_range": "Imported longitudes must be between -180 and 180.",
    "ck_dive_entry_position_pair": "An imported position needs both a latitude and a longitude.",
    "ck_dive_exit_position_pair": "An imported position needs both a latitude and a longitude.",
    # Reachable through the form, unlike the block above: a diver can type an average
    # deeper than their maximum. `validate_depth_pair` catches every route into it that
    # can see both numbers, so this is the backstop for the one that cannot - a PATCH
    # carrying one depth against a stored other half is checked before the write, and a
    # concurrent edit between that check and the UPDATE lands here.
    "ck_dive_avg_depth_within_max": "Average depth cannot be greater than max depth.",
}


def _validate_merged_depth_pair(avg_depth: float | None, max_depth: float | None) -> None:
    """Enforce `avg_depth <= max_depth` on a PATCH's merged result.

    The same shape `patch_course` uses for its date range, and for the same reason:
    `DiveUpdate` cannot see a pair whose other half is already stored, and
    `ck_dive_avg_depth_within_max` would refuse the write with an `IntegrityError` rather
    than a sentence naming the fields.
    """
    try:
        validate_depth_pair(avg_depth, max_depth)
    except ValueError as e:
        raise UnprocessableEntityException(str(e)) from e


def _fk_error_detail(exc: IntegrityError) -> str:
    """Translate a dive `IntegrityError` into a message worth showing a diver.

    Covers both foreign keys (a trip/course/site/gear item/species that vanished between
    validation and insert) and the domain `CheckConstraint`s. Constraint violations surface from the DB
    layer, not Pydantic, so without this the caller would get a raw 500 instead of a
    sentence naming the field.
    """
    msg = str(exc.orig)
    if "dive_trip_id_fkey" in msg:
        return "Trip not found."
    if "dive_course_id_fkey" in msg:
        return "Course not found."
    if "dive_contact_id_fkey" in msg:
        return CONTACT_NOT_FOUND
    if "dive_site_id_fkey" in msg:
        return "Dive site not found."
    if "gear_item_id_fkey" in msg:
        return "Gear item not found."
    if "species_id_fkey" in msg:
        return "Species not found."
    for constraint, detail in _DIVE_CONSTRAINT_MESSAGES.items():
        if constraint in msg:
            return detail
    return "Invalid reference: a related record does not exist."


_MIXTURE_CONSTRAINT_MESSAGES = {
    "ck_dive_mixture_volume_positive": "Volume must be positive.",
    "ck_dive_mixture_oxygen_range": "Oxygen percentage must be between 0 and 100.",
    "ck_dive_mixture_helium_range": "Helium percentage must be between 0 and 100.",
    "ck_dive_mixture_oxygen_helium_sum": "Oxygen and helium percentages cannot sum to more than 100.",
    "ck_dive_mixture_pressure_order": "End pressure cannot be greater than start pressure.",
    # "above 0", not "between 0 and 350": a diver told the value must be between 0 and 350
    # has been told the 0 they just typed is legal.
    "ck_dive_mixture_start_pressure_range": "Start pressure must be above 0 and at most 350 bar.",
    "ck_dive_mixture_end_pressure_range": "End pressure must be between 0 and 350 bar.",
    "ck_dive_mixture_po2_limit_range": "Gas ppO2 limit must be between 0.4 and 2.0 bar.",
    "ck_dive_mixture_gas_number_non_negative": "Gas number cannot be negative.",
}


_RECORDING_CONSTRAINT_MESSAGES = {
    # Unreachable through any path that exists today, and here for the reason the imported
    # block in `_DIVE_CONSTRAINT_MESSAGES` is: both writers drop an inverted gradient-factor
    # pair before it can reach the column - the parsers in
    # `ParsedDecoModel._pair_the_gradient_factors`, the importer in its planner - so a
    # violation would mean one of those stopped working. Without a message the attach route
    # would answer a 500 to a diver whose file is perfectly importable apart from two
    # numbers, and a parser bug would be invisible in the response.
    "ck_dive_recording_deco_gf_low_within_high": (
        "This file's decompression settings are inconsistent: its low gradient factor is above its high one."
    ),
    # The readouts, on the same terms: the parse-side validators on `ParsedDiveSchema` null
    # every value these would refuse, so a violation is a parser unit bug.
    "ck_dive_recording_cns_start_non_negative": "Imported CNS values must be zero or positive.",
    "ck_dive_recording_cns_end_non_negative": "Imported CNS values must be zero or positive.",
    "ck_dive_recording_otu_start_non_negative": "Imported OTU values must be zero or positive.",
    "ck_dive_recording_otu_end_non_negative": "Imported OTU values must be zero or positive.",
    "ck_dive_recording_surface_pressure_range": "Imported surface pressure must be between 0.4 and 1.2 bar.",
}


def _recording_error_detail(exc: IntegrityError) -> str:
    """As `_fk_error_detail`, for the constraints on a recording's own columns.

    Its own table rather than an entry in the dive one, because the two are reached from
    different routes: a recording's columns are written by the attach path and by logbook
    import, never by a dive body, so a message about them would never be looked up there.
    """
    msg = str(exc.orig)
    for constraint, detail in _RECORDING_CONSTRAINT_MESSAGES.items():
        if constraint in msg:
            return detail
    return "This dive-computer file could not be stored: one of its values is not one this app can hold."


def _mixture_error_detail(exc: IntegrityError) -> str:
    """As `_fk_error_detail`, but for the gas-mixture constraints (oxygen/helium ranges,
    their sum, volume, pressure ordering). Separate because a mixture failure has to name
    the mixture rather than the dive.
    """
    msg = str(exc.orig)
    for constraint, detail in _MIXTURE_CONSTRAINT_MESSAGES.items():
        if constraint in msg:
            return detail
    return "Invalid gas mixture."


async def _link_updates(db: AsyncSession, values: DiveUpdateRequest, owner_id: int) -> dict[str, int | None]:
    """The `trip_id`/`course_id`/`contact_id` half of a PATCH's update data.

    Both are optional references the caller names by public uuid, and both have to tell an
    explicit `null` (detach) from an omitted key (leave alone) - which is what
    `model_fields_set` answers and the value alone cannot. A uuid that is not the caller's
    own resolves to `None` and is refused, so someone else's stays unprobeable.

    Lifted out of `patch_dive` rather than written inline twice: the second copy is what
    pushed that handler past the complexity ceiling, and two near-identical branches in a
    handler that long is exactly where the next reference would be added to only one of
    them.
    """
    updates: dict[str, int | None] = {}

    if "trip_uuid" in values.model_fields_set:
        if values.trip_uuid is None:
            updates["trip_id"] = None
        else:
            trip_id = await resolve_trip_id_for_user(db=db, trip_uuid=values.trip_uuid, user_id=owner_id)
            if trip_id is None:
                raise UnprocessableEntityException("Trip not found.")
            updates["trip_id"] = trip_id

    if "course_uuid" in values.model_fields_set:
        if values.course_uuid is None:
            updates["course_id"] = None
        else:
            course_id = await resolve_course_id_for_user(db=db, course_uuid=values.course_uuid, user_id=owner_id)
            if course_id is None:
                raise UnprocessableEntityException("Course not found.")
            updates["course_id"] = course_id

    if "contact_uuid" in values.model_fields_set:
        updates["contact_id"] = (
            None
            if values.contact_uuid is None
            else await resolve_contact_reference(db, contact_uuid=values.contact_uuid, user_id=owner_id)
        )

    return updates


async def _get_owned_dive(db: AsyncSession, uuid: uuid_pkg.UUID, current_user: dict) -> DiveReadInternal:
    """Fetch a dive by public uuid and assert the caller owns it.

    Thin wrapper over `fetch_owned_or_raise` - see there for why someone else's row reads
    as a 404 and, in particular, why this must run before any `@cache`-wrapped read
    helper.
    """
    return await fetch_owned_or_raise(
        db=db,
        crud=crud_dives,
        uuid=uuid,
        current_user=current_user,
        schema=DiveReadInternal,
        not_found_message="Dive not found",
    )


def _to_public_start_time(data: dict[str, Any]) -> dict[str, Any]:
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


# The internal keys a public dive drops in favour of the uuids it resolves them to.
_INTERNAL_KEYS = frozenset({"id", "user_id", "trip_id", "course_id", "contact_id"})


def _to_public_dive(
    db_dive: DiveReadInternal | dict[str, Any],
    *,
    user_uuid: uuid_pkg.UUID,
    trip_uuid: uuid_pkg.UUID | None,
    course_uuid: uuid_pkg.UUID | None,
    contact_uuid: uuid_pkg.UUID | None,
    dive_sites: list[DiveSiteInfo],
    gear_items: list[GearItemInfo],
) -> DiveRead:
    """Convert an internal dive representation (integer FKs) into its public shape
    (owning user, trip, training course and contact referenced by `uuid`)."""
    data = _to_public_start_time(db_dive if isinstance(db_dive, dict) else db_dive.model_dump())
    return DiveRead(
        **{k: v for k, v in data.items() if k not in _INTERNAL_KEYS},
        user_uuid=user_uuid,
        trip_uuid=trip_uuid,
        course_uuid=course_uuid,
        contact_uuid=contact_uuid,
        dive_sites=dive_sites,
        gear_items=gear_items,
    )


def _to_public_dive_with_mixtures(
    db_dive: DiveReadInternal | dict[str, Any],
    *,
    user_uuid: uuid_pkg.UUID,
    trip_uuid: uuid_pkg.UUID | None,
    course_uuid: uuid_pkg.UUID | None,
    contact_uuid: uuid_pkg.UUID | None,
    dive_sites: list[DiveSiteInfo],
    gear_items: list[GearItemInfo],
    mixtures: list[DiveMixtureRead],
    species: list[SpeciesInfo],
    recordings: list[RecordingRead] | None = None,
    attribution: ProfileGasAttribution | None = None,
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
    data = _to_public_start_time(db_dive if isinstance(db_dive, dict) else db_dive.model_dump())
    return DiveReadWithMixtures(
        **{k: v for k, v in data.items() if k not in _INTERNAL_KEYS},
        user_uuid=user_uuid,
        trip_uuid=trip_uuid,
        course_uuid=course_uuid,
        contact_uuid=contact_uuid,
        dive_sites=dive_sites,
        gear_items=gear_items,
        mixtures=mixtures,
        species=species,
        recordings=recordings or [],
        # Both callers of this function (creating a dive, and the cached single-dive
        # read) go through here, so gas use is derived in exactly one place. Safe to
        # compute before caching, unlike gear service status: nothing about it depends
        # on when it's read.
        gas_use=resolve_gas_use(
            duration=data["duration"],
            avg_depth=data["avg_depth"],
            mixtures=mixtures,
            attribution=attribution,
        ),
    )


@router.post("/dive/parse", response_model=ParsedDiveResponse)
async def parse_dive(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    file: Annotated[UploadFile, File(description="Dive-computer export file (Suunto XML or JSON, or a FIT file)")],
) -> ParsedDiveResponse:
    """Upload a dive-computer export file and receive the parsed dive data as JSON.

    Nothing is stored here - the bytes are parsed and dropped. What comes back alongside
    the dive is a `file_token` attesting that this parse happened: hand it to
    `POST /dive/{uuid}/recordings` with the same file, once the dive it pre-filled exists,
    and the export is kept against that dive.

    `matches` is the second thing that comes back: dives of the caller's whose recordings
    started near this file's, so a form can offer *attach there* instead of logging a
    second dive for a computer the diver was already wearing. It is the **loose** test -
    the start window alone, nearest first - because a form is asking a question rather than
    making a decision; the ones flagged `same_recording` passed the much narrower test that
    says this file is a second export of a record that dive already has.
    """
    if not file.filename:
        raise BadRequestException("Missing filename")

    content = await read_upload_within_limit(file, MAX_DIVE_FILE_SIZE)
    try:
        # Off the event loop: parsing is pure CPU with nothing awaited inside it, and the
        # FIT decoder is pure Python, roughly two orders of magnitude more CPU per byte
        # than the C-accelerated `json`/`expat` the Suunto parsers ride on (0.07 s for a
        # 2.8 MB JSON export, against ~2 s per MB of densely-encoded FIT). What actually
        # bounds the worst case is `_MAX_FRAMES`, not this: a 5 MB file of bare `record`
        # messages took ~10 s to decode before that cap, and ~1.7 s after.
        parser, parsed = await run_in_threadpool(parse_dive_file_with_parser, file.filename, content)
    except UnsupportedDiveFileError as exc:
        # 415 and 409 stay raw `HTTPException`s - unlike 400/403/404/422, `http_exceptions`
        # has no class for either code.
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except DiveParseError as exc:
        raise UnprocessableEntityException(str(exc)) from exc

    return ParsedDiveResponse(
        **parsed.model_dump(),
        file_token=create_dive_file_token(
            user_uuid=current_user["uuid"],
            sha256=hashlib.sha256(content).hexdigest(),
            parser_key=parser.key,
        ),
        matches=await _parse_matches(db, user_id=current_user["id"], parsed=parsed),
    )


async def _parse_matches(db: AsyncSession, *, user_id: int, parsed: ParsedDiveSchema) -> list[ParsedDiveMatch]:
    """The caller's dives this parsed file might belong to, nearest start first.

    Scoped to the caller's own recordings by the query itself - `load_candidates` filters on
    `user_id` - so there is no ownership decision here to get wrong, and no way for the
    window to reach another diver's log.

    A file with no start time matches nothing and the list is empty: every gate is anchored
    on the clock, and a header-only export with no timestamp gives them nothing to compare.
    """
    if parsed.start_time is None:
        return []
    try:
        start_time, offset_minutes = split_local_start_time(datetime.fromisoformat(parsed.start_time))
    except ValueError:
        # A parser that produced something `fromisoformat` cannot read. Not a reason to fail
        # the parse - the form still gets its values - just one with no clock to match on.
        return []

    incoming = RecordingFacts(
        device=device_of(parsed.device),
        start_time=start_time,
        utc_offset_minutes=offset_minutes,
        duration=parsed.duration,
        max_depth=parsed.max_depth,
    )
    # Nearest start first, so a form's default offer is the closest candidate rather than the
    # oldest. Sorted through `start_delta` rather than by a plain subtraction, because that
    # is the one function that knows whether a given pair is comparable as instants or only
    # as wall clocks - and a sort that got it wrong would put an eleven-hour "difference"
    # first for exactly the pair the *Clocks* rule exists for.
    nearest = sorted(
        (
            (delta, candidate)
            for candidate in await load_candidates(db, user_id=user_id, around=start_time)
            if is_same_dive_loose(incoming, candidate.facts)
            and (delta := start_delta(incoming, candidate.facts)) is not None
        ),
        key=lambda pair: pair[0],
    )
    return [
        ParsedDiveMatch(
            dive_uuid=candidate.dive_uuid,
            dive_number=candidate.dive_number,
            started_at=(
                None
                if candidate.facts.start_time is None
                else combine_start_time(candidate.facts.start_time, candidate.facts.utc_offset_minutes)
            ),
            recording_uuid=candidate.uuid,
            device=_match_device(candidate.facts.device),
            same_recording=is_same_recording(incoming, candidate.facts),
        )
        for _, candidate in nearest
    ]


def _match_device(identity: DeviceIdentity) -> ParsedDevice | None:
    """A candidate's device as the response's shape, or `None` when it named nothing.

    Only the members the gates compare survive `DeviceIdentity`, which is deliberate: a form
    naming the match says *which computer*, and firmware is not that.
    """
    if identity.is_empty and identity.dive_number is None:
        return None
    return ParsedDevice(
        brand=identity.brand, model=identity.model, serial=identity.serial, dive_number=identity.dive_number
    )


@router.post("/dive", response_model=DiveReadWithMixtures, status_code=201)
async def write_dive(
    request: Request,
    dive: DiveCreateRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveReadWithMixtures:
    """Log a dive, together with its gas mixtures, dive sites, gear and species in one request.

    Every referenced trip, training course, dive site and gear item must belong to the
    caller: one that doesn't - or doesn't exist - is a 422 naming which, not a 403, since
    from the caller's side the two are the same thing. Species are the exception, and only
    because the catalog is global: a species uuid needs to exist, but it belongs to nobody,
    so there is no ownership to fail. Dive sites keep the order given; index 0 is the
    primary site, and species keep the order they were spotted in. Values the DB's domain
    constraints reject (a non-positive duration, a mixture over 100%) also come back as 422
    with the offending field named.
    """
    trip_id: int | None = None
    if dive.trip_uuid is not None:
        trip_id = await resolve_trip_id_for_user(db=db, trip_uuid=dive.trip_uuid, user_id=current_user["id"])
        if trip_id is None:
            raise UnprocessableEntityException("Trip not found.")

    course_id: int | None = None
    if dive.course_uuid is not None:
        course_id = await resolve_course_id_for_user(db=db, course_uuid=dive.course_uuid, user_id=current_user["id"])
        if course_id is None:
            raise UnprocessableEntityException("Course not found.")

    contact_id: int | None = None
    if dive.contact_uuid is not None:
        contact_id = await resolve_contact_reference(db, contact_uuid=dive.contact_uuid, user_id=current_user["id"])

    site_id_by_uuid = await resolve_dive_site_ids_for_user(
        db=db, dive_site_uuids=dive.dive_site_uuids, user_id=current_user["id"]
    )
    if site_id_by_uuid is None:
        raise UnprocessableEntityException("Dive site not found.")
    dive_site_ids = [site_id_by_uuid[u] for u in dive.dive_site_uuids]

    gear_id_by_uuid = await resolve_gear_item_ids_for_user(
        db=db, gear_item_uuids=dive.gear_item_uuids, user_id=current_user["id"]
    )
    if gear_id_by_uuid is None:
        raise UnprocessableEntityException("Gear item not found.")
    gear_item_ids = [gear_id_by_uuid[u] for u in dive.gear_item_uuids]

    # No user filter here, unlike the two above, and deliberately so: the species catalog is
    # global, so there is no owner to check against. See `resolve_species_ids`.
    species_id_by_uuid = await resolve_species_ids(db=db, species_uuids=dive.species_uuids)
    if species_id_by_uuid is None:
        raise UnprocessableEntityException("Species not found.")
    species_ids = [species_id_by_uuid[u] for u in dive.species_uuids]

    dive_internal_dict = dive.model_dump(
        exclude={
            "mixtures",
            "dive_site_uuids",
            "gear_item_uuids",
            "species_uuids",
            "trip_uuid",
            "course_uuid",
            "contact_uuid",
        }
    )
    utc_start_time, utc_offset_minutes = split_start_time(dive.start_time)
    dive_internal_dict["start_time"] = utc_start_time
    dive_internal = DiveCreateInternal(
        **dive_internal_dict,
        user_id=current_user["id"],
        trip_id=trip_id,
        course_id=course_id,
        contact_id=contact_id,
        utc_offset_minutes=utc_offset_minutes,
    )
    try:
        created_dive = await crud_dives.create(
            db=db, object=dive_internal, schema_to_select=DiveReadInternal, return_as_model=True
        )
    except IntegrityError as e:
        await db.rollback()
        raise UnprocessableEntityException(_fk_error_detail(e)) from e

    try:
        await replace_mixtures_for_dive(db=db, dive_id=created_dive.id, mixtures=dive.mixtures)
    except IntegrityError as e:
        await db.rollback()
        raise UnprocessableEntityException(_mixture_error_detail(e)) from e
    try:
        await replace_dive_sites_for_dive(db=db, dive_id=created_dive.id, dive_site_ids=dive_site_ids)
    except IntegrityError as e:
        await db.rollback()
        raise UnprocessableEntityException(_fk_error_detail(e)) from e
    try:
        await replace_gear_items_for_dive(db=db, dive_id=created_dive.id, gear_item_ids=gear_item_ids)
    except IntegrityError as e:
        await db.rollback()
        raise UnprocessableEntityException(_fk_error_detail(e)) from e
    try:
        await replace_species_for_dive(db=db, dive_id=created_dive.id, species_ids=species_ids)
    except IntegrityError as e:
        await db.rollback()
        raise UnprocessableEntityException(_fk_error_detail(e)) from e
    await recalculate_dive_stats(db=db, user_id=current_user["id"])
    await recalculate_gear_dive_counts(db=db, user_id=current_user["id"])
    await invalidate_dive_caches(current_user["id"])
    # Gear reads carry each item's `dive_count`, which this dive just changed.
    await invalidate_gear_caches(current_user["id"])

    dive_read_internal = await crud_dives.get(db=db, id=created_dive.id, schema_to_select=DiveReadInternal)
    if dive_read_internal is None:
        raise NotFoundException("Created dive not found")

    mixtures = await get_mixtures_for_dive(db=db, dive_id=created_dive.id)
    dive_sites = await get_dive_sites_for_dive(db=db, dive_id=created_dive.id)
    gear_items = await get_gear_items_for_dive(db=db, dive_id=created_dive.id)
    species = await get_species_for_dive(db=db, dive_id=created_dive.id)
    return _to_public_dive_with_mixtures(
        cast(dict[str, Any], dive_read_internal),
        user_uuid=current_user["uuid"],
        trip_uuid=dive.trip_uuid,
        course_uuid=dive.course_uuid,
        contact_uuid=dive.contact_uuid,
        dive_sites=dive_sites,
        gear_items=gear_items,
        mixtures=mixtures,
        species=species,
    )


# Every filter has to appear in this key, and every placeholder in it is looked up as a
# **keyword** argument - so a new filter passed positionally raises at key construction rather
# than quietly collapsing two different result sets onto one entry.
@cache(
    key_prefix=(
        "user_{user_id}_dives:page_{page}:items_per_page:{items_per_page}"
        ":trip_{trip_id}:course_{course_id}:site_{dive_site_id}:gear_{gear_item_id}:species_{species_id}"
    ),
    resource_id_name="user_id",
    expiration=60,
)
async def _cached_read_dives(
    request: Request,
    user_id: int,
    user_uuid: uuid_pkg.UUID,
    db: AsyncSession,
    page: int,
    items_per_page: int,
    trip_id: int | None,
    course_id: int | None,
    dive_site_id: int | None,
    gear_item_id: int | None,
    species_id: int | None,
) -> dict:
    """Fetches (and caches) a user's paginated dive list.

    This is only ever called after the caller's authorization has already been checked by
    `read_dives` below - it must not be called directly from a route, since the `@cache`
    decorator serves cached responses without re-running any authorization logic.

    Keyed and filtered by internal integer ids (rather than the caller-supplied uuids)
    since those are already known/resolved by the time this is called.
    """
    filters: dict[str, Any] = {"user_id": user_id, "is_deleted": False}
    if trip_id is not None:
        filters["trip_id"] = trip_id
    if course_id is not None:
        filters["course_id"] = course_id
    if dive_site_id is not None:
        # Match dives that include this site among their (possibly several) dive sites,
        # via a single `IN (subquery)` condition rather than resolving matching dive ids
        # in a separate round trip.
        filters["id__at_dive_site"] = dive_site_id
    if gear_item_id is not None:
        # Same shape as the dive site filter above: match dives that used this item.
        filters["id__with_gear_item"] = gear_item_id
    if species_id is not None:
        # Same shape again: match dives that recorded this species.
        filters["id__showing_species"] = species_id

    dives_data = await crud_dives.get_multi(
        db=db,
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
        sort_columns="start_time",
        sort_orders="desc",
        **filters,
    )

    # Enrich each dive with its dive site(s), gear and trip/course/contact uuids via batched
    # lookups.
    dive_ids = [d["id"] for d in dives_data["data"]]
    sites_by_dive = await get_dive_sites_for_dives(db=db, dive_ids=dive_ids)
    gear_by_dive = await get_gear_items_for_dives(db=db, dive_ids=dive_ids)
    referenced_trip_ids = [d["trip_id"] for d in dives_data["data"] if d["trip_id"] is not None]
    trip_uuid_by_id = await get_trip_uuids_by_ids(db=db, trip_ids=referenced_trip_ids, user_id=user_id)
    referenced_course_ids = [d["course_id"] for d in dives_data["data"] if d["course_id"] is not None]
    course_uuid_by_id = await get_course_uuids_by_ids(db=db, course_ids=referenced_course_ids, user_id=user_id)
    contact_by_id = await get_contact_refs_by_ids(
        db=db, contact_ids=[d["contact_id"] for d in dives_data["data"]], user_id=user_id
    )

    dives_data["data"] = [
        _to_public_dive(
            dive,
            user_uuid=user_uuid,
            trip_uuid=trip_uuid_by_id.get(dive["trip_id"]) if dive["trip_id"] is not None else None,
            course_uuid=course_uuid_by_id.get(dive["course_id"]) if dive["course_id"] is not None else None,
            contact_uuid=contact.uuid if (contact := contact_by_id.get(dive["contact_id"])) is not None else None,
            dive_sites=sites_by_dive.get(dive["id"], []),
            gear_items=gear_by_dive.get(dive["id"], []),
        ).model_dump()
        for dive in dives_data["data"]
    ]

    response: dict[str, Any] = paginated_response(crud_data=dives_data, page=page, items_per_page=items_per_page)
    return response


@router.get("/dives", response_model=PaginatedListResponse[DiveRead])
async def read_dives(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: int = 1,
    items_per_page: int = 10,
    trip_uuid: uuid_pkg.UUID | None = None,
    course_uuid: uuid_pkg.UUID | None = None,
    dive_site_uuid: uuid_pkg.UUID | None = None,
    gear_item_uuid: uuid_pkg.UUID | None = None,
    species_uuid: uuid_pkg.UUID | None = None,
) -> dict:
    """List the caller's dives, newest first, each with its trip, course, sites and gear.

    The `trip_uuid`, `course_uuid`, `dive_site_uuid`, `gear_item_uuid` and `species_uuid`
    filters are combinable, and one
    naming something that doesn't exist or isn't the caller's returns an empty page rather
    than an error - it reveals nothing about whether that resource exists. `dive_site_uuid`
    matches any dive that *includes* the site, since a dive can span several, and
    `species_uuid` any dive that recorded that species. Out-of-range pagination is clamped,
    not rejected.
    """
    page, items_per_page = clamp_pagination(page, items_per_page)

    trip_id: int | None = None
    if trip_uuid is not None:
        # -1 is a sentinel that can never match a real trip, so filtering safely
        # yields an empty result set for a nonexistent/foreign trip uuid.
        trip_id = await resolve_trip_id_for_user(db=db, trip_uuid=trip_uuid, user_id=current_user["id"]) or -1

    course_id: int | None = None
    if course_uuid is not None:
        # Same -1 sentinel as the trip filter above, for the same reason.
        course_id = await resolve_course_id_for_user(db=db, course_uuid=course_uuid, user_id=current_user["id"]) or -1

    dive_site_id: int | None = None
    if dive_site_uuid is not None:
        site_map = await resolve_dive_site_ids_for_user(
            db=db, dive_site_uuids=[dive_site_uuid], user_id=current_user["id"]
        )
        dive_site_id = (site_map or {}).get(dive_site_uuid, -1)

    gear_item_id: int | None = None
    if gear_item_uuid is not None:
        gear_map = await resolve_gear_item_ids_for_user(
            db=db, gear_item_uuids=[gear_item_uuid], user_id=current_user["id"]
        )
        gear_item_id = (gear_map or {}).get(gear_item_uuid, -1)

    species_id: int | None = None
    if species_uuid is not None:
        # Unlike its two neighbours this resolver takes no `user_id` - the species catalog is
        # global, so there is no owner to compare against (see `resolve_species_ids`). The -1
        # sentinel still applies, and it has to arrive through `(map or {})`: that function
        # returns **`None`, not an empty map**, when any uuid is unknown, so a bare `.get`
        # would raise `AttributeError` and answer 500 on exactly the unknown-uuid case this
        # filter has to answer with an empty page.
        species_map = await resolve_species_ids(db=db, species_uuids=[species_uuid])
        species_id = (species_map or {}).get(species_uuid, -1)

    return await _cached_read_dives(
        request,
        user_id=current_user["id"],
        user_uuid=current_user["uuid"],
        db=db,
        page=page,
        items_per_page=items_per_page,
        trip_id=trip_id,
        course_id=course_id,
        dive_site_id=dive_site_id,
        gear_item_id=gear_item_id,
        species_id=species_id,
    )


# -------------- numbering --------------
# All three of these are about the caller's own log as a whole rather than a page of it,
# which is why they sit beside `/user/dive-stats` and `/user/gas-use-history` in shape.
# `services/dive_numbering.py` carries the reasoning for what they do and, more to the
# point, for what they deliberately don't.


@router.get("/dives/next-number", response_model=DiveNumberSuggestion)
async def read_next_dive_number(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    start_time: Annotated[
        DiveStartTime,
        Query(description="The start time of the dive being logged, with its UTC offset"),
    ],
) -> DiveNumberSuggestion:
    """The dive number to prefill for a dive starting at `start_time`.

    A suggestion, not a reservation - nothing is held, and the client is free to ignore
    it. Not `@cache`d: it varies by `start_time`, so the keys would fan out per form
    keystroke to save two narrow indexed queries.
    """
    return await suggest_dive_number(db=db, user_id=current_user["id"], start_time=start_time)


# Keyed under the `user_{id}_dives:` prefix for the same reason as
# `_cached_gas_use_history` in `users.py`: `invalidate_dive_caches()` already sweeps
# `user_{id}_dives:*` after every dive create, update and delete, so this summary drops
# with them rather than being a third pattern to remember there. The renumber endpoint
# below calls that same helper, which is what keeps this from surviving its own fix.
@cache(key_prefix="user_{user_id}_dives:numbering", resource_id_name="user_id", expiration=60)
async def _cached_numbering_summary(request: Request, user_id: int, db: AsyncSession) -> DiveNumberingSummary:
    """Fetches (and caches) a user's dive-numbering summary. Authorization happens in the
    route before this is reached - `@cache` serves a hit without re-checking it.
    """
    return await summarize_numbering(db=db, user_id=user_id)


@router.get("/dives/numbering", response_model=DiveNumberingSummary)
async def read_dive_numbering(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveNumberingSummary:
    """The state of the caller's dive numbering: its range, its gaps, its duplicates, and
    whether it runs in date order.

    Purely descriptive. Gaps in particular are as often deliberate (a log that continues
    a paper logbook) as accidental, so this reports and the diver decides.
    """
    return await _cached_numbering_summary(request, user_id=current_user["id"], db=db)


@router.post("/dives/renumber", response_model=DiveRenumberResult)
async def renumber_user_dives(
    request: Request,
    values: DiveRenumberRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveRenumberResult:
    """Renumber the caller's dives consecutively, in chronological order.

    The one place in the app that rewrites numbers a diver entered, and it only ever runs
    when asked. Send `dry_run: true` first for the exact change list without writing.
    """
    result = await renumber_dives(
        db=db,
        user_id=current_user["id"],
        start_at=values.start_at,
        from_start_time=values.from_start_time,
        dry_run=values.dry_run,
    )

    if not result.dry_run and result.changes:
        # Only the dive caches. Unlike every other dive write, this one can't have moved
        # `recalculate_dive_stats`'s figures (count, max depth, total time) or any gear
        # item's `dive_count` - it changed a label on dives that already existed.
        await invalidate_dive_caches(current_user["id"])

    return result


# Keyed `user_{user_id}_dive:{uuid}` rather than the flat `dive_cache:{uuid}` it used to
# be. A dive read embeds its dive sites' and gear items' names, so renaming either has to
# drop the cached dives that reference it - and the renaming endpoint knows only the
# owner's id, not which of their dives are affected. Scoping the key by user is what makes
# `invalidate_dive_caches()` able to express that as a pattern at all.
#
# **This carried a `:v2` shape suffix for a day**, added when the decompression members
# landed and removed once the response cache became namespaced per build
# (`core/utils/cache.py`), which is the same property for every cached response and without
# anyone having to decide to have it. The reasoning is worth keeping even though the suffix
# is gone, because it is what the namespace has to be good enough to replace: every member
# that change added is defaulted - `RecordingRead.mode` and `.deco_model` are
# `Field(default=None)`, and `DiveProfileInfo.channels` was already a plain `list[str]`, so a
# short one still validates. An entry from the previous build therefore replays *cleanly* and
# tells a diver their dive has no mode, no model and four curves, for the whole of the hour
# this key lives. **A wrong answer, not the 500** that a required field with no default
# produces (`day` on `DiveActivityPoint`) - which is the failure a shape check on read would
# catch and this one would not, and why the namespace does not reason about shape at all.
#
# **If a suffix is ever wanted here again, it goes after the colon**, not after an
# underscore. `invalidate_dive_caches` sweeps `user_{id}_dives:*` and `user_{id}_dive:*` -
# two literal patterns rather than one `user_{id}_dive*`, deliberately, so the shorter one
# does not also eat the dive *site* list. A `user_{id}_dive_v2` falls outside both, and
# invalidation would silently stop working on this read.
@cache(key_prefix="user_{user_id}_dive", resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)
async def _cached_read_dive(
    request: Request, user_id: int, uuid: uuid_pkg.UUID, owner_uuid: uuid_pkg.UUID, db: AsyncSession
) -> DiveReadWithMixtures:
    """Fetches (and caches) a single dive by uuid.

    Like `_cached_read_dives`, this must only be called after authorization has already
    been checked, since `@cache` can serve a cached response without re-checking it.
    `user_id` is always the dive's owner (the route rejects anyone else), so it both
    scopes the cache key and can't be used to read another user's dive.
    """
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

    contact = (await get_contact_refs_by_ids(db=db, contact_ids=[db_dive["contact_id"]], user_id=user_id)).get(
        db_dive["contact_id"]
    )

    mixtures = await get_mixtures_for_dive(db=db, dive_id=db_dive["id"])
    dive_sites = await get_dive_sites_for_dive(db=db, dive_id=db_dive["id"])
    gear_items = await get_gear_items_for_dive(db=db, dive_id=db_dive["id"])
    species = await get_species_for_dive(db=db, dive_id=db_dive["id"])
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

    return _to_public_dive_with_mixtures(
        db_dive,
        user_uuid=owner_uuid,
        trip_uuid=trip_uuid,
        course_uuid=course_uuid,
        contact_uuid=None if contact is None else contact.uuid,
        dive_sites=dive_sites,
        gear_items=gear_items,
        mixtures=mixtures,
        species=species,
        recordings=recordings,
        attribution=attribution,
    )


@router.get("/dive/{uuid}", response_model=DiveReadWithMixtures)
async def read_dive(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveReadWithMixtures:
    """Return a single dive with its mixtures, sites, gear, species and recordings.

    404 when no such dive exists - and the same 404 when it belongs to another user, so
    someone else's uuid stays unprobeable.

    `recordings` is what recorded the dive, in order, the first primary. Each carries its
    device, its own start, the files it was read from and a summary of its samples - the
    samples themselves are not included, `GET /dive/{uuid}/recording/{rid}/profile` serving
    those separately since they are far larger than the rest of the dive put together.
    """
    await _get_owned_dive(db, uuid, current_user)

    return await _cached_read_dive(
        request, user_id=current_user["id"], uuid=uuid, owner_uuid=current_user["uuid"], db=db
    )


# Keyed `user_{id}_dives:neighbors:{uuid}` - under the *list* prefix rather than the
# `user_{id}_dive:` one a per-dive read would suggest, and for the reason `_cached_dive_activity`
# gives: this answer is about a dive's place among the others, so it goes stale when any
# of the owner's dives moves, not when this one changes. `invalidate_dive_caches()`
# already sweeps `user_{id}_dives:*` on every dive create, update and delete, which is
# exactly that set of events, so this needed no invalidation change of its own.
#
# The default hour rather than the 60s its siblings under this prefix use, and for the
# reason `_cached_read_dive` takes the default too: those are series a dashboard polls,
# where a short TTL is a cheap second line behind the invalidation, while this is a
# per-dive answer that only a dive write can change - and every one of those sweeps the
# prefix.
#
# Same authorization caveat as every `@cache`d helper here: a hit skips the body, so this
# must only ever be called after the route below has established the caller owns the dive.
@cache(key_prefix="user_{user_id}_dives:neighbors", resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)
async def _cached_read_dive_neighbors(
    request: Request, user_id: int, uuid: uuid_pkg.UUID, start_time: datetime, dive_id: int, db: AsyncSession
) -> DiveNeighbors:
    """Fetches (and caches) one dive's chronological neighbours. `uuid` is here to key the
    cache - the query itself runs off the `(start_time, dive_id)` position the route
    already resolved.
    """
    return await find_dive_neighbors(db=db, user_id=user_id, start_time=start_time, dive_id=dive_id)


@router.get("/dive/{uuid}/neighbors", response_model=DiveNeighbors)
async def read_dive_neighbors(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveNeighbors:
    """Return the dives immediately before and after this one in the caller's own log.

    Just enough of each to link to it - uuid, number and start time - so a dive page can
    offer prev/next without fetching either dive. `previous` is the one logged earlier and
    `next` the one logged later, which is the reverse of `GET /dives`, where the list runs
    newest first. Either is null at the ends of the log.

    Always the whole log: the `trip_uuid`/`course_uuid`/`dive_site_uuid`/`gear_item_uuid` filters on
    `GET /dives` have no counterpart here, so walking prev/next from a dive opened out of a
    filtered list leaves that filter behind at the first step.

    Chronology is `start_time`, never `dive_number` (see `services/dive_numbering.py`), and
    only the caller's own dives are ever neighbours. A dive that does not exist and one
    that belongs to another account are the same 404, exactly as `GET /dive/{uuid}`.
    """
    dive = await _get_owned_dive(db, uuid, current_user)

    return await _cached_read_dive_neighbors(
        request, user_id=current_user["id"], uuid=uuid, start_time=dive.start_time, dive_id=dive.id, db=db
    )


@router.patch("/dive/{uuid}")
async def patch_dive(
    request: Request,
    uuid: uuid_pkg.UUID,
    values: DiveUpdateRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Partially update a dive; omitted fields are left untouched.

    404 unless the caller owns it, exactly as for a dive that doesn't exist. The
    list-valued fields - `mixtures`, `dive_site_uuids`, `gear_item_uuids`, `species_uuids`
    - are replaced wholesale when present rather than merged, so sending a shorter list
    removes the difference and omitting the key entirely leaves it alone. Passing `null` for
    `trip_uuid` detaches the dive from its trip, which is distinct from omitting the key.
    Passing `null` for `course_uuid` detaches it from its training course the same way, and
    `null` for `contact_uuid` from its contact.
    Referencing anything the caller doesn't own is a 422, as are the DB's domain
    constraints.

    `start_time` normally has to carry a UTC offset. The exception is a dive whose offset
    is already unknown - an imported dive whose source recorded none - where an offsetless
    value like `2026-04-17T11:49:23` is accepted and leaves the offset unknown, so the wall
    clock stays editable without inventing one. Sending an offsetless value for a dive that
    *has* an offset is a 422: an update may preserve that state but never create it.

    A bare date like `2026-04-17` follows the same rule one level down: accepted only for a
    dive whose time of day is already unknown, keeping it so, and a 422 for any other. Any
    date-time ends that state.
    """
    db_dive = await _get_owned_dive(db, uuid, current_user)
    owner_id = db_dive.user_id

    update_data = values.model_dump(
        exclude={
            "mixtures",
            "dive_site_uuids",
            "gear_item_uuids",
            "species_uuids",
            "trip_uuid",
            "course_uuid",
            "contact_uuid",
        },
        exclude_unset=True,
    )

    # Keyed off `model_fields_set`, matching the reference branches in `_link_updates`,
    # so every optional-field branch on this path reads the same way. `DiveUpdate` rejects an
    # explicit null for `start_time` (the column is `NOT NULL`), so a field that is set
    # is always a real datetime here - which is what stops a null slipping past this
    # branch into the update and leaving `utc_offset_minutes` describing the *old* time.
    #
    # The offset and time-of-day rules are the stored dive's to answer, which is why they
    # are here and not on `DiveUpdate`: an offsetless value or a bare date preserves a state
    # the dive is already in and is refused on one that is not. See
    # `core/utils/datetime_offset.py`.
    if "start_time" in values.model_fields_set and values.start_time is not None:
        try:
            utc_start_time, utc_offset_minutes, date_only = split_updated_start_time(
                values.start_time, db_dive.utc_offset_minutes, db_dive.start_date_only
            )
        except ValueError as e:
            raise UnprocessableEntityException(str(e)) from e
        update_data["start_time"] = utc_start_time
        update_data["utc_offset_minutes"] = utc_offset_minutes
        update_data["start_date_only"] = date_only

    # Against the *merged* pair, since a PATCH may carry either depth alone - the case
    # neither `DiveUpdate`'s validator nor `DiveCreate`'s can see.
    _validate_merged_depth_pair(
        values.avg_depth if "avg_depth" in values.model_fields_set else db_dive.avg_depth,
        values.max_depth if "max_depth" in values.model_fields_set else db_dive.max_depth,
    )

    update_data.update(await _link_updates(db, values, owner_id))

    dive_site_ids: list[int] | None = None
    if values.dive_site_uuids is not None:
        site_id_by_uuid = await resolve_dive_site_ids_for_user(
            db=db, dive_site_uuids=values.dive_site_uuids, user_id=owner_id
        )
        if site_id_by_uuid is None:
            raise UnprocessableEntityException("Dive site not found.")
        dive_site_ids = [site_id_by_uuid[u] for u in values.dive_site_uuids]

    gear_item_ids: list[int] | None = None
    if values.gear_item_uuids is not None:
        gear_id_by_uuid = await resolve_gear_item_ids_for_user(
            db=db, gear_item_uuids=values.gear_item_uuids, user_id=owner_id
        )
        if gear_id_by_uuid is None:
            raise UnprocessableEntityException("Gear item not found.")
        gear_item_ids = [gear_id_by_uuid[u] for u in values.gear_item_uuids]

    species_ids: list[int] | None = None
    if values.species_uuids is not None:
        species_id_by_uuid = await resolve_species_ids(db=db, species_uuids=values.species_uuids)
        if species_id_by_uuid is None:
            raise UnprocessableEntityException("Species not found.")
        species_ids = [species_id_by_uuid[u] for u in values.species_uuids]

    if update_data:
        try:
            await crud_dives.update(db=db, object=update_data, uuid=uuid)
        except IntegrityError as e:
            await db.rollback()
            raise UnprocessableEntityException(_fk_error_detail(e)) from e

    dive_id = db_dive.id

    if values.mixtures is not None:
        try:
            await replace_mixtures_for_dive(db=db, dive_id=dive_id, mixtures=values.mixtures)
        except IntegrityError as e:
            await db.rollback()
            raise UnprocessableEntityException(_mixture_error_detail(e)) from e

    if dive_site_ids is not None:
        try:
            await replace_dive_sites_for_dive(db=db, dive_id=dive_id, dive_site_ids=dive_site_ids)
        except IntegrityError as e:
            await db.rollback()
            raise UnprocessableEntityException(_fk_error_detail(e)) from e

    if gear_item_ids is not None:
        try:
            await replace_gear_items_for_dive(db=db, dive_id=dive_id, gear_item_ids=gear_item_ids)
        except IntegrityError as e:
            await db.rollback()
            raise UnprocessableEntityException(_fk_error_detail(e)) from e

    if species_ids is not None:
        try:
            await replace_species_for_dive(db=db, dive_id=dive_id, species_ids=species_ids)
        except IntegrityError as e:
            await db.rollback()
            raise UnprocessableEntityException(_fk_error_detail(e)) from e

    if (
        update_data
        or values.mixtures is not None
        or dive_site_ids is not None
        or gear_item_ids is not None
        or species_ids is not None
    ):
        await recalculate_dive_stats(db=db, user_id=owner_id)
        await recalculate_gear_dive_counts(db=db, user_id=owner_id)
        await invalidate_dive_caches(owner_id)
        # Gear reads carry each item's `dive_count`, which this edit may have changed.
        await invalidate_gear_caches(owner_id)

    return {"message": "Dive updated"}


@router.delete("/dive/{uuid}")
async def erase_dive(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Soft-delete a dive, and hard-delete every recording and export stored against it.

    404 unless the caller owns it, exactly as for a dive that doesn't exist. The dive row
    is only flagged, but its recordings and their files are genuinely removed: leaving them
    would strand the bytes behind a dive nobody can open and hold each file's slot in the
    per-diver digest index, blocking a re-import of that same export into a fresh dive.
    """
    db_dive = await _get_owned_dive(db, uuid, current_user)
    owner_id = db_dive.user_id

    # The recordings go with the dive, and their files and profiles with them. The FK's
    # `ON DELETE CASCADE` from `dive` can't do this: `crud_dives.delete` sets `is_deleted`,
    # so no `DELETE FROM dive` ever runs. Leaving the rows would strand their bytes behind a
    # dive nobody can open, and would keep each file's slot in the per-diver digest index -
    # blocking a re-import of the same export into a fresh dive. Same reasoning as
    # `erase_certification`.
    await delete_files_for_dive(db=db, dive_id=db_dive.id, commit=False)
    await crud_dives.delete(db=db, uuid=uuid)
    await recalculate_dive_stats(db=db, user_id=owner_id)
    await recalculate_gear_dive_counts(db=db, user_id=owner_id)
    await invalidate_dive_caches(owner_id)
    # Gear reads carry each item's `dive_count`, which this dive no longer contributes to.
    await invalidate_gear_caches(owner_id)

    return {"message": "Dive deleted"}


@router.post("/dives/merge", response_model=DiveMergeResult)
async def merge_two_dives(
    request: Request,
    values: DiveMergeRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveMergeResult:
    """Fold two of your dives into one. **Not reversible.**

    For a computer that shut down mid-dive and logged the dive twice. The two records come
    into the app as two dives - no automatic match will ever fold them, one device's two
    records of one dive being exactly the pair the import gates refuse - so merging them is
    the diver's call.

    **The earlier dive survives**, by the same clock rule the match gates use: by instant
    where both dives record a UTC offset, by wall clock where either does not. The other
    dive's recordings, files, cylinders, sites, gear, species and notes move onto it and the
    dive itself is soft-deleted; its uuid stops resolving and nothing here can be undone.

    **What happens to the recordings depends on whether one computer or two recorded the
    dive.** The same computer's two records fold into a single recording: the later record's
    samples are offset onto the earlier record's clock by the delta between the two
    *recordings'* starts - never the two dives' - and the stretch where the computer was off
    stays an empty stretch, with no surface samples invented to bridge it. Whatever files
    either record kept stay downloadable on the one recording, and the folded samples are
    marked as a merge: nothing on this server can produce them again from those files.
    Two *different* computers were both recording throughout, so their recordings are simply
    appended side by side, the surviving dive's first.

    Either way the surviving dive's duration and maximum depth are re-seeded from what its
    recordings now carry, and a second computer's cylinder numbering is mapped onto this
    dive's own list.

    404 when either uuid is not a live dive of yours, exactly as for a dive that does not
    exist. 422 when the two are the same dive, when either was entered by hand and has no
    recording to fold, or when the surviving dive's average depth is deeper than anything
    the merged recordings actually reached.
    """
    first = await _get_owned_dive(db, values.dive_uuids[0], current_user)
    second = await _get_owned_dive(db, values.dive_uuids[1], current_user)

    try:
        merged = await merge_dives(db, first=first, second=second)
    except DiveNotMergeableError as exc:
        await db.rollback()
        raise UnprocessableEntityException(str(exc)) from exc
    except IntegrityError as exc:
        await db.rollback()
        raise UnprocessableEntityException(_fk_error_detail(exc)) from exc

    await db.commit()

    owner_id = first.user_id
    await recalculate_dive_stats(db=db, user_id=owner_id)
    # One dive fewer, and its gear items' `dive_count` with it - the same pair `erase_dive`
    # recalculates, for the same reason.
    await recalculate_gear_dive_counts(db=db, user_id=owner_id)
    await invalidate_dive_caches(owner_id)
    await invalidate_gear_caches(owner_id)

    return DiveMergeResult(
        dive=await _cached_read_dive(
            request, user_id=owner_id, uuid=merged.survivor_uuid, owner_uuid=current_user["uuid"], db=db
        ),
        removed_dive_uuid=merged.removed_uuid,
        folded=merged.folded,
    )


# -------------- recordings, and the files behind them --------------
# A dive's files are attached in a second request rather than riding along with
# `POST /dive`: that endpoint takes a JSON `DiveCreateRequest` (which is `extra="forbid"`),
# and turning the one resource-creating route in the app into a multipart one to carry an
# optional attachment is a poor trade.
#
# **`PUT /dive/{uuid}/file` is gone, and so are the three routes beside it.** They were
# whole-slot replace over a slot that no longer exists: a dive holds an ordered list of
# recordings and each holds a list of files, so there is nothing left for a `PUT` to
# replace. What they became:
#
#   PUT    /dive/{uuid}/file            ->  POST   /dive/{uuid}/recordings
#   GET    /dive/{uuid}/file            ->  GET    /dive/{uuid}/file/{fid}
#   DELETE /dive/{uuid}/file            ->  DELETE /dive/{uuid}/file/{fid}
#   GET    /dive/{uuid}/profile         ->  GET    /dive/{uuid}/recording/{rid}/profile
#
# `POST` rather than `PUT` on the first, deliberately: attaching is no longer idempotent in
# the HTTP sense - the same bytes twice are still a no-op, but two *different* files are two
# additions rather than a replacement - and a `PUT` that appended would be a lie about the
# method. Nothing was deployed anywhere when this was written and the web client moved in the
# same change, so nothing is aliased.


@router.post("/dive/{uuid}/recordings", response_model=RecordingRead, status_code=201)
async def write_dive_recording(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    file: Annotated[UploadFile, File(description="A dive-computer export of this dive")],
    file_token: Annotated[str, Form(description="The `file_token` returned by `POST /dive/parse` for this file")],
) -> RecordingRead:
    """Attach a dive-computer export to this dive, in the recording it belongs to.

    The token is what admits the file: it proves this server parsed these exact bytes for
    this user, so the endpoint neither has to re-parse nor has to trust that an arbitrary
    upload is a dive log at all.

    Returns the **recording** the file landed in, not just the file - because which of them
    it landed in is the server's decision and the caller has to be told. A file whose device
    and start match one of this dive's existing recordings is a second export of that same
    record (the same computer's JSON and FIT, say) and fills its blanks without overwriting
    anything; anything else is a second computer and gets a recording of its own, appended
    after the last.

    The same bytes twice is a no-op returning the recording they are already in. Bytes
    already stored against *another* dive of this account are a 409 naming it: the realistic
    cause is logging one export as two dives, and saying so is more use than either silent
    fix.
    """
    db_dive = await _get_owned_dive(db, uuid, current_user)

    try:
        stored = await store_recording_file(
            db=db,
            user_id=current_user["id"],
            user_uuid=current_user["uuid"],
            dive_id=db_dive.id,
            upload=file,
            file_token=file_token,
        )
    except InvalidDiveFileTokenError as exc:
        raise UnprocessableEntityException(str(exc)) from exc
    except DiveFileAlreadyLinkedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DiveFileConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        # A parsed value the schemas let through and the database will not take. Every
        # writable column here is filled from a file rather than from a body, so this is a
        # parser bug rather than a diver's mistake - but a 500 would say nothing at all, and
        # the message names the field so the file can be reported.
        raise UnprocessableEntityException(_recording_error_detail(exc)) from exc

    # Dive reads embed every recording, its files' metadata *and* the summary of the profile
    # extracted from them, so they're now stale.
    await invalidate_dive_caches(current_user["id"])

    # Read back after the write rather than returned from it: the response is the *recording*
    # - its device, its files, its profile summary - and assembling that is this layer's job
    # rather than the storage service's. A caller that got only the file back would have to
    # issue this very query itself to render anything.
    recordings = (await get_recordings_for_dives(db=db, dive_ids=[db_dive.id])).get(db_dive.id, [])
    return next(recording for recording in recordings if any(file.uuid == stored.file_uuid for file in recording.files))


@router.get("/dive/{uuid}/file/{fid}")
async def read_dive_file(
    request: Request,
    uuid: uuid_pkg.UUID,
    fid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    v: Annotated[
        str | None,
        Query(description="Opaque cache-busting version token; ignored by the server"),
    ] = None,
) -> Response:
    """Serve one of a dive's stored exports back to its owner.

    Deliberately *not* `@cache`d, for the same reason as the certification card download:
    Redis here holds serialized API responses, and parking multi-megabyte binaries in it
    would evict everything else the cache exists for. The `ETag`/`If-None-Match` pair does
    the equivalent job in the browser.

    `fid` is the file's own uuid, from the dive's `recordings[].files[]`. A uuid that is not
    one of *this* dive's files is a 404 whether it exists elsewhere or not.

    `v` is read by nothing here; it is declared so the contract is visible. The response is
    cacheable for five minutes, so the client varies `v` to give each version its own cache
    entry.
    """
    db_dive = await _get_owned_dive(db, uuid, current_user)

    try:
        file_id, _ = await resolve_dive_file(db=db, dive_id=db_dive.id, uuid=fid)
    except DiveFileNotFoundError as exc:
        raise NotFoundException(str(exc)) from exc

    # Check the hash before loading the bytes, so a conditional request costs one narrow
    # query rather than a full read that gets thrown away.
    sha256 = await get_dive_file_sha256(db=db, file_id=file_id)
    if sha256 is None:
        raise NotFoundException("This dive has no such file")

    etag = f'"{sha256}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, max-age=300"})

    file = await load_dive_file(db=db, file_id=file_id)
    if file is None:
        raise NotFoundException("This dive has no such file")

    return Response(
        content=file.data,
        media_type=file.content_type,
        headers={
            # `attachment`, not `inline`: the web app fetches this through its API client
            # and hands it to the browser as a download, so it never navigates here. XML
            # opened in a tab at the app's own origin is exactly what we don't want.
            "Content-Disposition": content_disposition_attachment(file.original_filename, default="dive-file"),
            # The stored type comes from the parser that read the file, but say so
            # explicitly: the browser must not be free to re-interpret user-uploaded
            # content as something scriptable.
            "X-Content-Type-Options": "nosniff",
            # `frame-ancestors` is spelled out because it does not fall back to
            # `default-src`: a response with its own policy opts out of
            # `SecurityHeadersMiddleware`'s default and would otherwise be framable
            # however strict the rest of this is.
            "Content-Security-Policy": "default-src 'none'; sandbox; frame-ancestors 'none'",
            # `private` because this is one diver's file and no shared cache should keep
            # a copy; the `ETag` makes re-validation after 5 minutes cheap.
            "Cache-Control": "private, max-age=300",
            "ETag": etag,
        },
    )


@router.get("/dive/{uuid}/recording/{rid}/profile", response_model=RecordingProfileRead)
async def read_dive_profile(
    request: Request,
    uuid: uuid_pkg.UUID,
    rid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    v: Annotated[
        str | None,
        Query(description="Opaque cache-busting version token; ignored by the server"),
    ] = None,
) -> Response | RecordingProfileRead:
    """Serve one recording's per-sample curves and the events alongside them.

    Ten channels: what its sensors read - depth, deco ceiling, temperature and per-cylinder
    tank pressure - and what its computer worked out from them - the no-decompression clock,
    the time to surface, the computed ppO2, the CNS clock and the two gradient factors. A
    recording carries whichever of them its files recorded, and `channels` on the dive read
    says which without fetching the samples.

    Per **recording**, not per dive: a diver on two computers has two profiles of one dive
    and neither is a version of the other. `rid` is the recording's uuid, from the dive's
    `recordings[]`; the first of those is the primary one, which is what a client showing a
    single chart should draw. The `times` are elapsed milliseconds from that recording's own
    `started_at`, which is why a recording carries a start of its own.

    `provenance` says where the samples came from - read off this recording's files,
    supplied by an imported document, or folded from two recordings by a merge. The same
    value rides `recordings[].profile` on `GET /dive/{uuid}`, so a client that has the dive
    read already need not fetch the samples to ask.

    Deliberately *not* `@cache`d, and for a sharper reason than the file route above. A
    profile is **immutable** for a given (source digest, extractor version) pair, which makes
    it the ideal `ETag` case and the worst Redis case: every dive cache key lives under
    `user_{id}_dive*` and `invalidate_dive_caches` sweeps the lot on every dive edit and
    every dive-site or gear rename - none of which can change a profile. Caching it would
    mean evicting and refetching tens of KB per dive for nothing.

    `v` is read by nothing here; it is declared so the contract is visible. The client varies
    it with the profile's `updated_at` so a re-extraction gets its own cache entry rather
    than being masked by the previous one for five minutes.
    """
    db_dive = await _get_owned_dive(db, uuid, current_user)

    try:
        recording_id = await resolve_recording(db=db, dive_id=db_dive.id, uuid=rid)
    except RecordingNotFoundError as exc:
        raise NotFoundException(str(exc)) from exc

    # The version before the payload, so a conditional request costs one two-column query
    # rather than decoding tens of KB of JSONB only to throw it away.
    version = await get_profile_version(db=db, recording_id=recording_id)
    if version is None:
        raise NotFoundException("This recording has no profile")

    etag = f'"{version}"'
    if request.headers.get("if-none-match") == etag:
        # Returning a bare `Response` bypasses `response_model` validation, which a 304
        # with no body would otherwise fail - the same thing `read_dive_file` relies on.
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, max-age=300"})

    profile = await load_profile(db=db, recording_id=recording_id)
    if profile is None:
        raise NotFoundException("This recording has no profile")

    # Set here rather than left to `ClientCacheMiddleware`, which never overrides a
    # `Cache-Control` an endpoint set for itself.
    response = JSONResponse(content=jsonable_encoder(to_recording_read_schema(profile)))
    response.headers["Cache-Control"] = "private, max-age=300"
    response.headers["ETag"] = etag
    return response


@router.delete("/dive/{uuid}/file/{fid}")
async def erase_dive_file(
    request: Request,
    uuid: uuid_pkg.UUID,
    fid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Delete one stored export, leaving the dive itself.

    404 unless the caller owns the dive, exactly as for a dive that doesn't exist, and 404
    again when the dive has no such file - so this is not idempotent: a repeat delete reports
    the absence rather than succeeding quietly.

    **What goes with the file is what was only ever read off it**, and how much that is
    depends on what is left. The recording's profile and readouts are re-derived from its
    remaining files, and so are the dive's entry and exit fixes if this was the primary
    recording. A recording whose last file goes normally goes with it. The exception is a
    recording whose profile no file could re-yield - one a merge produced, or one a document
    supplied - which survives its last file's deletion along with its samples.

    The dive keeps everything that went through the form, its cylinders included.
    """
    db_dive = await _get_owned_dive(db, uuid, current_user)

    try:
        file_id, _ = await resolve_dive_file(db=db, dive_id=db_dive.id, uuid=fid)
        await delete_dive_file(db=db, file_id=file_id)
    except DiveFileNotFoundError as exc:
        raise NotFoundException(str(exc)) from exc

    await invalidate_dive_caches(current_user["id"])
    return {"message": "Dive file deleted"}


@router.delete("/dive/{uuid}/recording/{rid}")
async def erase_dive_recording(
    request: Request,
    uuid: uuid_pkg.UUID,
    rid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Delete a whole recording - its files, its samples and what was derived from them.

    The only way to remove a recording that holds no files, which is what a converted logbook
    import creates and what a merge can leave behind: there is no file whose deletion would
    take it.

    Removing the primary recording promotes the next one, and the dive's entry and exit fixes
    are re-derived from whatever becomes primary. Removing a **secondary** one leaves them
    exactly as they are: they were never read off that recording, and rewriting them from a
    primary this deletion did not touch is a loss rather than a repair - see
    `refresh_tech_scalars`. 404 unless the caller owns the dive, and 404 again when it has no
    such recording.
    """
    db_dive = await _get_owned_dive(db, uuid, current_user)

    try:
        recording_id = await resolve_recording(db=db, dive_id=db_dive.id, uuid=rid)
    except RecordingNotFoundError as exc:
        raise NotFoundException(str(exc)) from exc

    # Asked before the delete, which renumbers the ordinals and takes the answer with it.
    touched_primary = (await primary_recording_ids(db=db, dive_ids=[db_dive.id])).get(db_dive.id) == recording_id

    await delete_recording(db=db, recording_id=recording_id, dive_id=db_dive.id, commit=False)
    await refresh_tech_scalars(db=db, dive_id=db_dive.id, touched_primary=touched_primary)
    await db.commit()

    await invalidate_dive_caches(current_user["id"])
    return {"message": "Dive recording deleted"}


@router.patch("/dive/{uuid}/recording/{rid}", response_model=RecordingRead)
async def patch_dive_recording(
    request: Request,
    uuid: uuid_pkg.UUID,
    rid: uuid_pkg.UUID,
    values: RecordingUpdateRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> RecordingRead:
    """Make one of a dive's recordings the primary one.

    `{"primary": true}` moves it to ordinal 0 and shifts the rest down, keeping their order.
    That decides three things at once: which profile and readouts a single-chart client
    shows, which recording's files write the dive's entry and exit fixes - re-derived here
    from the new primary - and which one a one-profile-per-dive export writes.

    `{"primary": false}` is refused with a 422 rather than silently ignored: there is no
    "make this one *not* primary" operation, because something has to be, and the diver means
    to promote a different one.
    """
    db_dive = await _get_owned_dive(db, uuid, current_user)

    try:
        recording_id = await resolve_recording(db=db, dive_id=db_dive.id, uuid=rid)
        await make_primary(db=db, recording_id=recording_id, dive_id=db_dive.id)
    except RecordingNotFoundError as exc:
        raise NotFoundException(str(exc)) from exc

    # A promotion is the one caller that always touched the primary: that is what it did.
    await refresh_tech_scalars(db=db, dive_id=db_dive.id, touched_primary=True)
    await db.commit()
    await invalidate_dive_caches(current_user["id"])

    recordings = (await get_recordings_for_dives(db=db, dive_ids=[db_dive.id])).get(db_dive.id, [])
    return next(recording for recording in recordings if recording.uuid == rid)
