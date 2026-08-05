import uuid as uuid_pkg
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import ForbiddenException, NotFoundException
from ...core.utils.cache import cache, delete_keys_by_pattern
from ...core.utils.datetime_offset import combine_start_time, split_start_time
from ...crud.crud_dive_dive_sites import (
    get_dive_sites_for_dive,
    get_dive_sites_for_dives,
    replace_dive_sites_for_dive,
)
from ...crud.crud_dive_mixtures import get_mixtures_for_dive, replace_mixtures_for_dive
from ...crud.crud_dive_sites import resolve_dive_site_ids_for_user
from ...crud.crud_dives import crud_dives
from ...crud.crud_trips import get_trip_uuids_by_ids, resolve_trip_id_for_user
from ...schemas.dive import (
    DiveCreateInternal,
    DiveCreateRequest,
    DiveRead,
    DiveReadInternal,
    DiveReadWithMixtures,
    DiveSiteInfo,
    DiveUpdateRequest,
)
from ...schemas.dive_mixture import DiveMixtureRead
from ...schemas.parsed_dive import ParsedDiveSchema
from ...services.dive_parsers import DiveParseError, UnsupportedDiveFileError, parse_dive_file
from ...services.dive_stats import recalculate_dive_stats

router = APIRouter(tags=["dives"])

# Dive-computer export files are small (samples are a few bytes each); this cap is
# generous headroom while still bounding memory usage and XML-parser workload for an
# endpoint that accepts arbitrary user-uploaded files.
_MAX_DIVE_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
_MAX_DIVE_FILE_SIZE_MB = _MAX_DIVE_FILE_SIZE // (1024 * 1024)
_UPLOAD_READ_CHUNK_SIZE = 1024 * 1024  # 1 MB


async def _read_upload_within_limit(file: UploadFile, max_size: int) -> bytes:
    """Read an upload's full content, rejecting it once it exceeds `max_size`.

    Reads in bounded chunks instead of trusting the `Content-Length` header (which
    may be absent or spoofed) or calling `file.read()` unbounded, so at most
    `max_size` (+ one chunk) bytes are ever buffered in memory.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_READ_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_size:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Maximum allowed size is {_MAX_DIVE_FILE_SIZE_MB} MB.",
            )
        chunks.append(chunk)
    return b"".join(chunks)


_DIVE_CONSTRAINT_MESSAGES = {
    "ck_dive_duration_positive": "Duration must be positive.",
    "ck_dive_visibility_non_negative": "Visibility must be zero or positive.",
    "ck_dive_max_depth_positive": "Max depth must be positive.",
    "ck_dive_avg_depth_positive": "Average depth must be positive.",
}


def _fk_error_detail(exc: IntegrityError) -> str:
    msg = str(exc.orig)
    if "dive_trip_id_fkey" in msg:
        return "Trip not found."
    if "dive_site_id_fkey" in msg:
        return "Dive site not found."
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
}


def _mixture_error_detail(exc: IntegrityError) -> str:
    msg = str(exc.orig)
    for constraint, detail in _MIXTURE_CONSTRAINT_MESSAGES.items():
        if constraint in msg:
            return detail
    return "Invalid gas mixture."


def _dive_owner_id(db_dive: Any) -> int:
    return cast(int, db_dive["user_id"] if isinstance(db_dive, dict) else db_dive.user_id)


def _dive_internal_id(db_dive: Any) -> int:
    return cast(int, db_dive["id"] if isinstance(db_dive, dict) else db_dive.id)


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
) -> DiveRead:
    """Convert an internal dive representation (integer FKs) into its public shape
    (owning user and trip referenced by `uuid`)."""
    data = _to_public_start_time(db_dive if isinstance(db_dive, dict) else db_dive.model_dump())
    return DiveRead(
        **{k: v for k, v in data.items() if k not in ("id", "user_id", "trip_id")},
        user_uuid=user_uuid,
        trip_uuid=trip_uuid,
        dive_sites=dive_sites,
    )


def _to_public_dive_with_mixtures(
    db_dive: DiveReadInternal | dict[str, Any],
    *,
    user_uuid: uuid_pkg.UUID,
    trip_uuid: uuid_pkg.UUID | None,
    dive_sites: list[DiveSiteInfo],
    mixtures: list[DiveMixtureRead],
) -> DiveReadWithMixtures:
    data = _to_public_start_time(db_dive if isinstance(db_dive, dict) else db_dive.model_dump())
    return DiveReadWithMixtures(
        **{k: v for k, v in data.items() if k not in ("id", "user_id", "trip_id")},
        user_uuid=user_uuid,
        trip_uuid=trip_uuid,
        dive_sites=dive_sites,
        mixtures=mixtures,
    )


@router.post("/dive/parse-xml", response_model=ParsedDiveSchema, dependencies=[Depends(get_current_user)])
async def parse_dive_xml(
    file: Annotated[UploadFile, File(description="Dive-computer export file (e.g. Suunto XML)")],
) -> ParsedDiveSchema:
    """Upload a dive-computer export file and receive the parsed dive data as JSON."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    content = await _read_upload_within_limit(file, _MAX_DIVE_FILE_SIZE)
    try:
        return parse_dive_file(file.filename, content)
    except UnsupportedDiveFileError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except DiveParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/dive", response_model=DiveReadWithMixtures, status_code=201)
async def write_dive(
    request: Request,
    dive: DiveCreateRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveReadWithMixtures:
    if current_user["uuid"] != dive.user_uuid:
        raise ForbiddenException()

    trip_id: int | None = None
    if dive.trip_uuid is not None:
        trip_id = await resolve_trip_id_for_user(db=db, trip_uuid=dive.trip_uuid, user_id=current_user["id"])
        if trip_id is None:
            raise HTTPException(status_code=422, detail="Trip not found.")

    site_id_by_uuid = await resolve_dive_site_ids_for_user(
        db=db, dive_site_uuids=dive.dive_site_uuids, user_id=current_user["id"]
    )
    if site_id_by_uuid is None:
        raise HTTPException(status_code=422, detail="Dive site not found.")
    dive_site_ids = [site_id_by_uuid[u] for u in dive.dive_site_uuids]

    dive_internal_dict = dive.model_dump(exclude={"mixtures", "dive_site_uuids", "user_uuid", "trip_uuid"})
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
        raise HTTPException(status_code=422, detail=_fk_error_detail(e)) from e

    try:
        await replace_mixtures_for_dive(db=db, dive_id=created_dive.id, mixtures=dive.mixtures)
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status_code=422, detail=_mixture_error_detail(e)) from e
    try:
        await replace_dive_sites_for_dive(db=db, dive_id=created_dive.id, dive_site_ids=dive_site_ids)
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status_code=422, detail=_fk_error_detail(e)) from e
    await recalculate_dive_stats(db=db, user_id=current_user["id"])
    await delete_keys_by_pattern(f"user_{current_user['id']}_dives:*")

    dive_read_internal = await crud_dives.get(db=db, id=created_dive.id, schema_to_select=DiveReadInternal)
    if dive_read_internal is None:
        raise NotFoundException("Created dive not found")

    mixtures = await get_mixtures_for_dive(db=db, dive_id=created_dive.id)
    dive_sites = await get_dive_sites_for_dive(db=db, dive_id=created_dive.id)
    return _to_public_dive_with_mixtures(
        cast(dict[str, Any], dive_read_internal),
        user_uuid=current_user["uuid"],
        trip_uuid=dive.trip_uuid,
        dive_sites=dive_sites,
        mixtures=mixtures,
    )


@cache(
    key_prefix=("user_{user_id}_dives:page_{page}:items_per_page:{items_per_page}:trip_{trip_id}:site_{dive_site_id}"),
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

    dives_data = await crud_dives.get_multi(
        db=db,
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
        sort_columns="start_time",
        sort_orders="desc",
        **filters,
    )

    # Enrich each dive with its dive site(s) and trip uuid via batched lookups.
    dive_ids = [d["id"] for d in dives_data["data"]]
    sites_by_dive = await get_dive_sites_for_dives(db=db, dive_ids=dive_ids)
    referenced_trip_ids = [d["trip_id"] for d in dives_data["data"] if d["trip_id"] is not None]
    trip_uuid_by_id = await get_trip_uuids_by_ids(db=db, trip_ids=referenced_trip_ids)

    dives_data["data"] = [
        _to_public_dive(
            dive,
            user_uuid=user_uuid,
            trip_uuid=trip_uuid_by_id.get(dive["trip_id"]) if dive["trip_id"] is not None else None,
            dive_sites=sites_by_dive.get(dive["id"], []),
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
) -> dict:
    if current_user["uuid"] != user_uuid:
        raise ForbiddenException()

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

    return await _cached_read_dives(
        request,
        user_id=current_user["id"],
        user_uuid=user_uuid,
        db=db,
        page=page,
        items_per_page=items_per_page,
        trip_id=trip_id,
        dive_site_id=dive_site_id,
    )


@cache(key_prefix="dive_cache", resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)
async def _cached_read_dive(
    request: Request, uuid: uuid_pkg.UUID, owner_uuid: uuid_pkg.UUID, db: AsyncSession
) -> DiveReadWithMixtures:
    """Fetches (and caches) a single dive by uuid, regardless of owner.

    Like `_cached_read_dives`, this must only be called after authorization has already
    been checked, since `@cache` can serve a cached response without re-checking it.
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
    return _to_public_dive_with_mixtures(
        db_dive, user_uuid=owner_uuid, trip_uuid=trip_uuid, dive_sites=dive_sites, mixtures=mixtures
    )


@router.get("/dive/{uuid}", response_model=DiveReadWithMixtures)
async def read_dive(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveReadWithMixtures:
    db_dive = await crud_dives.get(db=db, uuid=uuid, is_deleted=False, schema_to_select=DiveReadInternal)
    if db_dive is None:
        raise NotFoundException("Dive not found")

    if _dive_owner_id(db_dive) != current_user["id"]:
        raise ForbiddenException()

    return await _cached_read_dive(request, uuid=uuid, owner_uuid=current_user["uuid"], db=db)


@router.patch("/dive/{uuid}")
@cache("dive_cache", resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)
async def patch_dive(
    request: Request,
    uuid: uuid_pkg.UUID,
    values: DiveUpdateRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    db_dive = await crud_dives.get(db=db, uuid=uuid, is_deleted=False, schema_to_select=DiveReadInternal)
    if db_dive is None:
        raise NotFoundException("Dive not found")

    owner_id = _dive_owner_id(db_dive)
    if owner_id != current_user["id"]:
        raise ForbiddenException()

    update_data = values.model_dump(exclude={"mixtures", "dive_site_uuids", "trip_uuid"}, exclude_unset=True)

    if values.start_time is not None:
        utc_start_time, utc_offset_minutes = split_start_time(values.start_time)
        update_data["start_time"] = utc_start_time
        update_data["utc_offset_minutes"] = utc_offset_minutes

    if "trip_uuid" in values.model_fields_set:
        if values.trip_uuid is None:
            update_data["trip_id"] = None
        else:
            trip_id = await resolve_trip_id_for_user(db=db, trip_uuid=values.trip_uuid, user_id=owner_id)
            if trip_id is None:
                raise HTTPException(status_code=422, detail="Trip not found.")
            update_data["trip_id"] = trip_id

    dive_site_ids: list[int] | None = None
    if values.dive_site_uuids is not None:
        site_id_by_uuid = await resolve_dive_site_ids_for_user(
            db=db, dive_site_uuids=values.dive_site_uuids, user_id=owner_id
        )
        if site_id_by_uuid is None:
            raise HTTPException(status_code=422, detail="Dive site not found.")
        dive_site_ids = [site_id_by_uuid[u] for u in values.dive_site_uuids]

    if update_data:
        try:
            await crud_dives.update(db=db, object=update_data, uuid=uuid)
        except IntegrityError as e:
            await db.rollback()
            raise HTTPException(status_code=422, detail=_fk_error_detail(e)) from e

    dive_id = _dive_internal_id(db_dive)

    if values.mixtures is not None:
        try:
            await replace_mixtures_for_dive(db=db, dive_id=dive_id, mixtures=values.mixtures)
        except IntegrityError as e:
            await db.rollback()
            raise HTTPException(status_code=422, detail=_mixture_error_detail(e)) from e

    if dive_site_ids is not None:
        try:
            await replace_dive_sites_for_dive(db=db, dive_id=dive_id, dive_site_ids=dive_site_ids)
        except IntegrityError as e:
            await db.rollback()
            raise HTTPException(status_code=422, detail=_fk_error_detail(e)) from e

    if update_data or values.mixtures is not None or dive_site_ids is not None:
        await recalculate_dive_stats(db=db, user_id=owner_id)
        await delete_keys_by_pattern(f"user_{owner_id}_dives:*")

    return {"message": "Dive updated"}


@router.delete("/dive/{uuid}")
@cache("dive_cache", resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)
async def erase_dive(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    db_dive = await crud_dives.get(db=db, uuid=uuid, is_deleted=False, schema_to_select=DiveReadInternal)
    if db_dive is None:
        raise NotFoundException("Dive not found")

    owner_id = _dive_owner_id(db_dive)
    if owner_id != current_user["id"]:
        raise ForbiddenException()

    await crud_dives.delete(db=db, uuid=uuid)
    await recalculate_dive_stats(db=db, user_id=owner_id)
    await delete_keys_by_pattern(f"user_{owner_id}_dives:*")

    return {"message": "Dive deleted"}
