import hashlib
import uuid as uuid_pkg
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
    ForbiddenException,
    NotFoundException,
    UnprocessableEntityException,
)
from ...core.security import create_dive_file_token
from ...core.utils.cache import cache
from ...core.utils.datetime_offset import combine_start_time, split_start_time
from ...core.utils.pagination import clamp_pagination
from ...core.utils.uploads import content_disposition_attachment, read_upload_within_limit
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
from ...crud.crud_dives import crud_dives
from ...crud.crud_gear_items import resolve_gear_item_ids_for_user
from ...crud.crud_trips import get_trip_uuids_by_ids, resolve_trip_id_for_user
from ...schemas.dive import (
    DiveCreateInternal,
    DiveCreateRequest,
    DiveFileInfo,
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
)
from ...schemas.dive_mixture import DiveMixtureRead
from ...schemas.dive_profile import DiveProfileInfo, DiveProfileRead
from ...schemas.gear_item import GearItemInfo
from ...schemas.parsed_dive import ParsedDiveResponse
from ...services.cache_invalidation import invalidate_dive_caches, invalidate_gear_caches
from ...services.dive_files import (
    MAX_DIVE_FILE_SIZE,
    DiveFileAlreadyLinkedError,
    DiveFileConflictError,
    InvalidDiveFileTokenError,
    delete_dive_file,
    delete_files_for_dive,
    get_dive_file_sha256,
    get_file_infos_for_dives,
    load_dive_file,
    store_dive_file,
)
from ...services.dive_gas import resolve_gas_use
from ...services.dive_numbering import renumber_dives, suggest_dive_number, summarize_numbering
from ...services.dive_parsers import DiveParseError, UnsupportedDiveFileError, parse_dive_file_with_parser
from ...services.dive_profiles import (
    ProfileGasAttribution,
    get_gas_attribution_for_dives,
    get_profile_infos_for_dives,
    get_profile_version,
    load_profile,
    to_read_schema,
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
    # Unreachable through the form - these columns are written only by the import path
    # (see DECISIONS.md) - but a constraint with no message here surfaces as a raw 500,
    # and the whole point of the map is that the DB is the backstop for whatever reaches
    # it. A violation would mean a parser unit bug, so the messages say so.
    "ck_dive_cns_start_non_negative": "Imported CNS values must be zero or positive.",
    "ck_dive_cns_end_non_negative": "Imported CNS values must be zero or positive.",
    "ck_dive_otu_start_non_negative": "Imported OTU values must be zero or positive.",
    "ck_dive_otu_end_non_negative": "Imported OTU values must be zero or positive.",
    "ck_dive_surface_pressure_range": "Imported surface pressure must be between 0.5 and 1.2 bar.",
}


def _fk_error_detail(exc: IntegrityError) -> str:
    """Translate a dive `IntegrityError` into a message worth showing a diver.

    Covers both foreign keys (a trip/site/gear item that vanished between validation and
    insert) and the domain `CheckConstraint`s. Constraint violations surface from the DB
    layer, not Pydantic, so without this the caller would get a raw 500 instead of a
    sentence naming the field.
    """
    msg = str(exc.orig)
    if "dive_trip_id_fkey" in msg:
        return "Trip not found."
    if "dive_site_id_fkey" in msg:
        return "Dive site not found."
    if "gear_item_id_fkey" in msg:
        return "Gear item not found."
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
    "ck_dive_mixture_po2_limit_range": "Gas ppO2 limit must be between 0.4 and 2.0 bar.",
    "ck_dive_mixture_gas_number_non_negative": "Gas number cannot be negative.",
}


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
    offset key, so the public `DiveRead`/`DiveReadWithMixtures` shape always exposes a single
    offset-aware `start_time` (e.g. `2021-04-04T10:04:47.910+02:00`) - see
    `core/utils/datetime_offset.py`.
    """
    data = dict(data)
    offset_minutes = data.pop("utc_offset_minutes")
    data["start_time"] = combine_start_time(data["start_time"], offset_minutes)
    return data


def _to_public_dive(
    db_dive: DiveReadInternal | dict[str, Any],
    *,
    user_uuid: uuid_pkg.UUID,
    trip_uuid: uuid_pkg.UUID | None,
    dive_sites: list[DiveSiteInfo],
    gear_items: list[GearItemInfo],
) -> DiveRead:
    """Convert an internal dive representation (integer FKs) into its public shape
    (owning user and trip referenced by `uuid`)."""
    data = _to_public_start_time(db_dive if isinstance(db_dive, dict) else db_dive.model_dump())
    return DiveRead(
        **{k: v for k, v in data.items() if k not in ("id", "user_id", "trip_id")},
        user_uuid=user_uuid,
        trip_uuid=trip_uuid,
        dive_sites=dive_sites,
        gear_items=gear_items,
    )


def _to_public_dive_with_mixtures(
    db_dive: DiveReadInternal | dict[str, Any],
    *,
    user_uuid: uuid_pkg.UUID,
    trip_uuid: uuid_pkg.UUID | None,
    dive_sites: list[DiveSiteInfo],
    gear_items: list[GearItemInfo],
    mixtures: list[DiveMixtureRead],
    source_file: DiveFileInfo | None = None,
    profile: DiveProfileInfo | None = None,
    attribution: ProfileGasAttribution | None = None,
) -> DiveReadWithMixtures:
    """Assemble a dive's public shape from the row plus everything a read embeds.

    Internal FKs are dropped in favour of the related resources' uuids and summaries, so
    the related rows are passed in already fetched - the caller batches them across a page
    rather than querying per dive.

    `attribution` is the one input here that never reaches the response as itself: it is
    the profile's account of which cylinder was breathed when, and it exists solely so a
    multi-cylinder dive can produce `gas_use`. `None` on the create path, where the dive
    cannot yet have a file to have been extracted from.
    """
    data = _to_public_start_time(db_dive if isinstance(db_dive, dict) else db_dive.model_dump())
    return DiveReadWithMixtures(
        **{k: v for k, v in data.items() if k not in ("id", "user_id", "trip_id")},
        user_uuid=user_uuid,
        trip_uuid=trip_uuid,
        dive_sites=dive_sites,
        gear_items=gear_items,
        mixtures=mixtures,
        source_file=source_file,
        profile=profile,
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
    file: Annotated[UploadFile, File(description="Dive-computer export file (Suunto XML or JSON, or a FIT file)")],
) -> ParsedDiveResponse:
    """Upload a dive-computer export file and receive the parsed dive data as JSON.

    Nothing is stored here - the bytes are parsed and dropped. What comes back alongside
    the dive is a `file_token` attesting that this parse happened: hand it to
    `PUT /dive/{uuid}/file` with the same file, once the dive it pre-filled exists, and
    the export is kept against that dive.
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
    )


@router.post("/dive", response_model=DiveReadWithMixtures, status_code=201)
async def write_dive(
    request: Request,
    dive: DiveCreateRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveReadWithMixtures:
    """Log a dive, together with its gas mixtures, dive sites and gear in one request.

    `user_uuid` must be the caller's own (403 otherwise). Every referenced trip, dive site
    and gear item must belong to the caller too: one that doesn't - or doesn't exist - is
    a 422 naming which, not a 403, since from the caller's side the two are the same
    thing. Dive sites keep the order given; index 0 is the primary site. Values the DB's
    domain constraints reject (a non-positive duration, a mixture over 100%) also come
    back as 422 with the offending field named.
    """
    if current_user["uuid"] != dive.user_uuid:
        raise ForbiddenException()

    trip_id: int | None = None
    if dive.trip_uuid is not None:
        trip_id = await resolve_trip_id_for_user(db=db, trip_uuid=dive.trip_uuid, user_id=current_user["id"])
        if trip_id is None:
            raise UnprocessableEntityException("Trip not found.")

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

    dive_internal_dict = dive.model_dump(
        exclude={"mixtures", "dive_site_uuids", "gear_item_uuids", "user_uuid", "trip_uuid"}
    )
    utc_start_time, utc_offset_minutes = split_start_time(dive.start_time)
    dive_internal_dict["start_time"] = utc_start_time
    dive_internal = DiveCreateInternal(
        **dive_internal_dict, user_id=current_user["id"], trip_id=trip_id, utc_offset_minutes=utc_offset_minutes
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
    return _to_public_dive_with_mixtures(
        cast(dict[str, Any], dive_read_internal),
        user_uuid=current_user["uuid"],
        trip_uuid=dive.trip_uuid,
        dive_sites=dive_sites,
        gear_items=gear_items,
        mixtures=mixtures,
    )


@cache(
    key_prefix=(
        "user_{user_id}_dives:page_{page}:items_per_page:{items_per_page}"
        ":trip_{trip_id}:site_{dive_site_id}:gear_{gear_item_id}"
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
    dive_site_id: int | None,
    gear_item_id: int | None,
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
    if dive_site_id is not None:
        # Match dives that include this site among their (possibly several) dive sites,
        # via a single `IN (subquery)` condition rather than resolving matching dive ids
        # in a separate round trip.
        filters["id__at_dive_site"] = dive_site_id
    if gear_item_id is not None:
        # Same shape as the dive site filter above: match dives that used this item.
        filters["id__with_gear_item"] = gear_item_id

    dives_data = await crud_dives.get_multi(
        db=db,
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
        sort_columns="start_time",
        sort_orders="desc",
        **filters,
    )

    # Enrich each dive with its dive site(s), gear and trip uuid via batched lookups.
    dive_ids = [d["id"] for d in dives_data["data"]]
    sites_by_dive = await get_dive_sites_for_dives(db=db, dive_ids=dive_ids)
    gear_by_dive = await get_gear_items_for_dives(db=db, dive_ids=dive_ids)
    referenced_trip_ids = [d["trip_id"] for d in dives_data["data"] if d["trip_id"] is not None]
    trip_uuid_by_id = await get_trip_uuids_by_ids(db=db, trip_ids=referenced_trip_ids)

    dives_data["data"] = [
        _to_public_dive(
            dive,
            user_uuid=user_uuid,
            trip_uuid=trip_uuid_by_id.get(dive["trip_id"]) if dive["trip_id"] is not None else None,
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
    user_uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: int = 1,
    items_per_page: int = 10,
    trip_uuid: uuid_pkg.UUID | None = None,
    dive_site_uuid: uuid_pkg.UUID | None = None,
    gear_item_uuid: uuid_pkg.UUID | None = None,
) -> dict:
    """List the caller's dives, newest first, each with its trip, sites and gear attached.

    `user_uuid` must be the caller's own (403 otherwise). The `trip_uuid`,
    `dive_site_uuid` and `gear_item_uuid` filters are combinable, and one naming something
    that doesn't exist or isn't the caller's returns an empty page rather than an error -
    it reveals nothing about whether that resource exists. `dive_site_uuid` matches any
    dive that *includes* the site, since a dive can span several. Out-of-range pagination
    is clamped, not rejected.
    """
    if current_user["uuid"] != user_uuid:
        raise ForbiddenException()

    page, items_per_page = clamp_pagination(page, items_per_page)

    trip_id: int | None = None
    if trip_uuid is not None:
        # -1 is a sentinel that can never match a real trip, so filtering safely
        # yields an empty result set for a nonexistent/foreign trip uuid.
        trip_id = await resolve_trip_id_for_user(db=db, trip_uuid=trip_uuid, user_id=current_user["id"]) or -1

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

    return await _cached_read_dives(
        request,
        user_id=current_user["id"],
        user_uuid=user_uuid,
        db=db,
        page=page,
        items_per_page=items_per_page,
        trip_id=trip_id,
        dive_site_id=dive_site_id,
        gear_item_id=gear_item_id,
    )


# -------------- numbering --------------
# All three of these are about the caller's own log as a whole, so - unlike `GET /dives`
# and every other route here - they take no `user_uuid`, matching `/user/dive-stats` and
# `/user/gas-use-history`. `services/dive_numbering.py` carries the reasoning for what
# they do and, more to the point, for what they deliberately don't.


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


# Keyed `user_{user_id}_dive:{uuid}` rather than the flat `dive_cache:{uuid}` it used
# to be. A dive read embeds its dive sites' and gear items' names, so renaming either
# has to drop the cached dives that reference it - and the renaming endpoint knows only
# the owner's id, not which of their dives are affected. Scoping the key by user is what
# makes `invalidate_dive_caches()` able to express that as a pattern at all.
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
        trip_uuid_by_id = await get_trip_uuids_by_ids(db=db, trip_ids=[db_dive["trip_id"]])
        trip_uuid = trip_uuid_by_id.get(db_dive["trip_id"])

    mixtures = await get_mixtures_for_dive(db=db, dive_id=db_dive["id"])
    dive_sites = await get_dive_sites_for_dive(db=db, dive_id=db_dive["id"])
    gear_items = await get_gear_items_for_dive(db=db, dive_id=db_dive["id"])
    source_files = await get_file_infos_for_dives(db=db, dive_ids=[db_dive["id"]])
    # Written batched though only ever called with one id, matching `get_file_infos_for_dives`.
    profiles = await get_profile_infos_for_dives(db=db, dive_ids=[db_dive["id"]])
    # A second narrow read of the same row, rather than a column on the summary above: this
    # one is never serialized, and it is the query `gas_use_history` needs on its own over
    # a whole log. See `get_gas_attribution_for_dives`.
    #
    # Only for a dive that can use it, which the mixtures just read already say. Anything
    # else - every single-cylinder dive, and every dive logged without a cylinder at all -
    # would be paying a round trip against the row `get_profile_infos_for_dives` just read
    # for a value `compute_multi_tank_gas_use` discards on its first line.
    attribution = None
    if len(mixtures) >= 2:
        attribution = (await get_gas_attribution_for_dives(db=db, dive_ids=[db_dive["id"]]))[db_dive["id"]]

    return _to_public_dive_with_mixtures(
        db_dive,
        user_uuid=owner_uuid,
        trip_uuid=trip_uuid,
        dive_sites=dive_sites,
        gear_items=gear_items,
        mixtures=mixtures,
        source_file=source_files.get(db_dive["id"]),
        profile=profiles.get(db_dive["id"]),
        attribution=attribution,
    )


@router.get("/dive/{uuid}", response_model=DiveReadWithMixtures)
async def read_dive(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveReadWithMixtures:
    """Return a single dive with its mixtures, sites, gear, source file and profile summary.

    404 when no such dive exists - and the same 404 when it belongs to another user, so
    someone else's uuid stays unprobeable. The profile's
    samples are not included - `GET /dive/{uuid}/profile` serves those separately, since
    they are far larger than the rest of the dive put together.
    """
    await _get_owned_dive(db, uuid, current_user)

    return await _cached_read_dive(
        request, user_id=current_user["id"], uuid=uuid, owner_uuid=current_user["uuid"], db=db
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
    list-valued fields - `mixtures`,
    `dive_site_uuids`, `gear_item_uuids` - are replaced wholesale when present rather than
    merged, so sending a shorter list removes the difference and omitting the key entirely
    leaves it alone. Passing `null` for `trip_uuid` detaches the dive from its trip, which
    is distinct from omitting the key. Referencing anything the caller doesn't own is a
    422, as are the DB's domain constraints.
    """
    db_dive = await _get_owned_dive(db, uuid, current_user)
    owner_id = db_dive.user_id

    update_data = values.model_dump(
        exclude={"mixtures", "dive_site_uuids", "gear_item_uuids", "trip_uuid"}, exclude_unset=True
    )

    # Keyed off `model_fields_set`, matching the `trip_uuid` branch below, so the two
    # optional-field branches in this route read the same way. `DiveUpdate` rejects an
    # explicit null for `start_time` (the column is `NOT NULL`), so a field that is set
    # is always a real datetime here - which is what stops a null slipping past this
    # branch into the update and leaving `utc_offset_minutes` describing the *old* time.
    if "start_time" in values.model_fields_set and values.start_time is not None:
        utc_start_time, utc_offset_minutes = split_start_time(values.start_time)
        update_data["start_time"] = utc_start_time
        update_data["utc_offset_minutes"] = utc_offset_minutes

    if "trip_uuid" in values.model_fields_set:
        if values.trip_uuid is None:
            update_data["trip_id"] = None
        else:
            trip_id = await resolve_trip_id_for_user(db=db, trip_uuid=values.trip_uuid, user_id=owner_id)
            if trip_id is None:
                raise UnprocessableEntityException("Trip not found.")
            update_data["trip_id"] = trip_id

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

    if update_data or values.mixtures is not None or dive_site_ids is not None or gear_item_ids is not None:
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
    """Soft-delete a dive, and hard-delete the dive-computer export stored against it.

    404 unless the caller owns it, exactly as for a dive that doesn't exist. The dive row
    is only flagged, but its source file is
    genuinely removed: leaving it would strand the bytes behind a dive nobody can open and
    hold the file's slot in the unique indexes, blocking a re-import of that same export
    into a fresh dive.
    """
    db_dive = await _get_owned_dive(db, uuid, current_user)
    owner_id = db_dive.user_id

    # The stored export goes with the dive. The FK's `ON DELETE CASCADE` can't do this:
    # `crud_dives.delete` sets `is_deleted`, so no `DELETE FROM dive` ever runs. Leaving
    # the row would strand its bytes behind a dive nobody can open, and would keep the
    # file's slot in both unique indexes - blocking a re-import of the same export into
    # a fresh dive. Same reasoning as `erase_certification`.
    await delete_files_for_dive(db=db, dive_id=db_dive.id, commit=False)
    await crud_dives.delete(db=db, uuid=uuid)
    await recalculate_dive_stats(db=db, user_id=owner_id)
    await recalculate_gear_dive_counts(db=db, user_id=owner_id)
    await invalidate_dive_caches(owner_id)
    # Gear reads carry each item's `dive_count`, which this dive no longer contributes to.
    await invalidate_gear_caches(owner_id)

    return {"message": "Dive deleted"}


# -------------- source files --------------
# The export a dive was imported from is attached in a second request rather than riding
# along with `POST /dive`: that endpoint takes a JSON `DiveCreateRequest` (which is
# `extra="forbid"`), and turning the one resource-creating route in the app into a
# multipart one to carry an optional attachment is a poor trade. A separate `PUT` is
# also idempotent, which is what makes re-importing the same file harmless.


@router.put("/dive/{uuid}/file", response_model=DiveFileInfo)
async def write_dive_file(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    file: Annotated[UploadFile, File(description="The dive-computer export this dive was imported from")],
    file_token: Annotated[str, Form(description="The `file_token` returned by `POST /dive/parse` for this file")],
) -> DiveFileInfo:
    """Attach or replace the export this dive was imported from.

    The token is what admits the file: it proves this server parsed these exact bytes
    for this user, so the endpoint neither has to re-parse nor has to trust that an
    arbitrary upload is a dive log at all.
    """
    db_dive = await _get_owned_dive(db, uuid, current_user)

    try:
        info = await store_dive_file(
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

    # Dive reads embed this file's metadata *and* the summary of the profile extracted
    # from it (`store_dive_file` does that in the same transaction), so they're now stale.
    await invalidate_dive_caches(current_user["id"])
    return info


@router.get("/dive/{uuid}/file")
async def read_dive_file(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    v: Annotated[
        str | None,
        Query(description="Opaque cache-busting version token; ignored by the server"),
    ] = None,
) -> Response:
    """Serve a dive's stored export back to its owner.

    Deliberately *not* `@cache`d, for the same reason as the certification card
    download: Redis here holds serialized API responses, and parking multi-megabyte
    binaries in it would evict everything else the cache exists for. The
    `ETag`/`If-None-Match` pair does the equivalent job in the browser.

    `v` is read by nothing here; it is declared so the contract is visible. The response
    is cacheable for five minutes and the file at this URL can be *replaced*, so the
    client varies `v` to give each version its own cache entry.
    """
    db_dive = await _get_owned_dive(db, uuid, current_user)
    dive_id = db_dive.id

    # Check the hash before loading the bytes, so a conditional request costs one narrow
    # query rather than a full read that gets thrown away.
    sha256 = await get_dive_file_sha256(db=db, dive_id=dive_id)
    if sha256 is None:
        raise NotFoundException("This dive has no source file")

    etag = f'"{sha256}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, max-age=300"})

    file = await load_dive_file(db=db, dive_id=dive_id)
    if file is None:
        raise NotFoundException("This dive has no source file")

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
            "Content-Security-Policy": "default-src 'none'; sandbox",
            # `private` because this is one diver's file and no shared cache should keep
            # a copy; the `ETag` makes re-validation after 5 minutes cheap.
            "Cache-Control": "private, max-age=300",
            "ETag": etag,
        },
    )


@router.get("/dive/{uuid}/profile", response_model=DiveProfileRead)
async def read_dive_profile(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    v: Annotated[
        str | None,
        Query(description="Opaque cache-busting version token; ignored by the server"),
    ] = None,
) -> Response | DiveProfileRead:
    """Serve a dive's per-sample depth/ceiling/temperature/tank-pressure curves and its events.

    Deliberately *not* `@cache`d, and for a sharper reason than the file route above. A
    profile is **immutable** for a given (source file, extractor version) pair, which
    makes it the ideal `ETag` case and the worst Redis case: every dive cache key lives
    under `user_{id}_dive*` and `invalidate_dive_caches` sweeps the lot on every dive
    edit and every dive-site or gear rename - none of which can change a profile. Caching
    it would mean evicting and refetching tens of KB per dive for nothing.

    `v` is read by nothing here; it is declared so the contract is visible. The client
    varies it with the profile's `updated_at` so a re-extraction gets its own cache entry
    rather than being masked by the previous one for five minutes.
    """
    db_dive = await _get_owned_dive(db, uuid, current_user)
    dive_id = db_dive.id

    # The version before the payload, so a conditional request costs one two-column query
    # rather than decoding tens of KB of JSONB only to throw it away.
    version = await get_profile_version(db=db, dive_id=dive_id)
    if version is None:
        raise NotFoundException("This dive has no profile")

    etag = f'"{version}"'
    if request.headers.get("if-none-match") == etag:
        # Returning a bare `Response` bypasses `response_model` validation, which a 304
        # with no body would otherwise fail - the same thing `read_dive_file` relies on.
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, max-age=300"})

    profile = await load_profile(db=db, dive_id=dive_id)
    if profile is None:
        raise NotFoundException("This dive has no profile")

    # Set here rather than left to `ClientCacheMiddleware`, which never overrides a
    # `Cache-Control` an endpoint set for itself.
    response = JSONResponse(content=jsonable_encoder(to_read_schema(profile)))
    response.headers["Cache-Control"] = "private, max-age=300"
    response.headers["ETag"] = etag
    return response


@router.delete("/dive/{uuid}/file")
async def erase_dive_file(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Delete the stored dive-computer export from a dive, leaving the dive itself.

    404 unless the caller owns it, exactly as for a dive that doesn't exist, and 404
    again when the dive has no source file - so this is not idempotent: a repeat delete
    reports the absence rather than succeeding quietly.

    The dive keeps everything that went through the form, its cylinders included. What
    goes with the file is what was only ever read *off* it: the extracted profile, and the
    CNS, OTU and surface-pressure readings - none of which can be re-derived or checked
    against anything once the export is gone.
    """
    db_dive = await _get_owned_dive(db, uuid, current_user)

    deleted = await delete_dive_file(db=db, dive_id=db_dive.id)
    if not deleted:
        raise NotFoundException("This dive has no source file")

    await invalidate_dive_caches(current_user["id"])
    return {"message": "Dive file deleted"}
