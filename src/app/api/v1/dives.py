from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import ForbiddenException, NotFoundException
from ...core.utils.cache import cache, delete_keys_by_pattern
from ...crud.crud_dive_dive_sites import (
    get_dive_ids_for_dive_site,
    get_dive_sites_for_dive,
    get_dive_sites_for_dives,
    replace_dive_sites_for_dive,
)
from ...crud.crud_dive_mixtures import get_mixtures_for_dive, replace_mixtures_for_dive
from ...crud.crud_dive_sites import dive_site_ids_belong_to_user
from ...crud.crud_dives import crud_dives
from ...crud.crud_trips import trip_belongs_to_user
from ...schemas.dive import (
    DiveCreateInternal,
    DiveCreateRequest,
    DiveRead,
    DiveReadWithMixtures,
    DiveUpdateRequest,
)
from ...schemas.parsed_dive import ParsedDiveSchema
from ...services.dive_parsers import DiveParseError, UnsupportedDiveFileError, parse_dive_file
from ...services.dive_stats import recalculate_dive_stats

router = APIRouter(tags=["dives"])


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
}


def _mixture_error_detail(exc: IntegrityError) -> str:
    msg = str(exc.orig)
    for constraint, detail in _MIXTURE_CONSTRAINT_MESSAGES.items():
        if constraint in msg:
            return detail
    return "Invalid gas mixture."


def _dive_owner_id(db_dive: Any) -> int:
    return cast(int, db_dive["user_id"] if isinstance(db_dive, dict) else db_dive.user_id)


@router.post("/dive/parse-xml", response_model=ParsedDiveSchema, dependencies=[Depends(get_current_user)])
async def parse_dive_xml(
    file: Annotated[UploadFile, File(description="Dive-computer export file (e.g. Suunto XML)")],
) -> ParsedDiveSchema:
    """Upload a dive-computer export file and receive the parsed dive data as JSON."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    content = await file.read()
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
    if current_user["id"] != dive.user_id:
        raise ForbiddenException()

    if dive.trip_id is not None and not await trip_belongs_to_user(db=db, trip_id=dive.trip_id, user_id=dive.user_id):
        raise HTTPException(status_code=422, detail="Trip not found.")

    if not await dive_site_ids_belong_to_user(db=db, dive_site_ids=dive.dive_site_ids, user_id=dive.user_id):
        raise HTTPException(status_code=422, detail="Dive site not found.")

    dive_internal_dict = dive.model_dump(exclude={"mixtures", "dive_site_ids"})

    dive_internal = DiveCreateInternal(**dive_internal_dict)
    try:
        created_dive = await crud_dives.create(
            db=db, object=dive_internal, schema_to_select=DiveRead, return_as_model=True
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
        await replace_dive_sites_for_dive(db=db, dive_id=created_dive.id, dive_site_ids=dive.dive_site_ids)
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status_code=422, detail=_fk_error_detail(e)) from e
    await recalculate_dive_stats(db=db, user_id=dive.user_id)
    await delete_keys_by_pattern(f"user_{dive.user_id}_dives:*")

    dive_read = await crud_dives.get(db=db, id=created_dive.id, schema_to_select=DiveRead)
    if dive_read is None:
        raise NotFoundException("Created dive not found")

    mixtures = await get_mixtures_for_dive(db=db, dive_id=created_dive.id)
    dive_sites = await get_dive_sites_for_dive(db=db, dive_id=created_dive.id)
    return DiveReadWithMixtures(**cast(dict[str, Any], dive_read), mixtures=mixtures, dive_sites=dive_sites)


@cache(
    key_prefix=("user_{user_id}_dives:page_{page}:items_per_page:{items_per_page}:trip_{trip_id}:site_{dive_site_id}"),
    resource_id_name="user_id",
    expiration=60,
)
async def _cached_read_dives(
    request: Request,
    user_id: int,
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
    """
    filters: dict[str, Any] = {"user_id": user_id, "is_deleted": False}
    if trip_id is not None:
        filters["trip_id"] = trip_id
    if dive_site_id is not None:
        # Match dives that include this site among their (possibly several) dive
        # sites. `[-1]` is a sentinel that safely yields an empty result set when
        # no dive references this site, rather than relying on `IN ()` semantics.
        filters["id__in"] = await get_dive_ids_for_dive_site(db=db, dive_site_id=dive_site_id) or [-1]

    dives_data = await crud_dives.get_multi(
        db=db,
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
        sort_columns="start_time",
        sort_orders="desc",
        **filters,
    )

    # Enrich each dive with its dive site(s) via a single batched lookup.
    sites_by_dive = await get_dive_sites_for_dives(db=db, dive_ids=[d["id"] for d in dives_data["data"]])
    for dive in dives_data["data"]:
        dive["dive_sites"] = sites_by_dive.get(dive["id"], [])

    response: dict[str, Any] = paginated_response(crud_data=dives_data, page=page, items_per_page=items_per_page)
    return response


@router.get("/dives", response_model=PaginatedListResponse[DiveRead])
async def read_dives(
    request: Request,
    user_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: int = 1,
    items_per_page: int = 10,
    trip_id: int | None = None,
    dive_site_id: int | None = None,
) -> dict:
    if current_user["id"] != user_id:
        raise ForbiddenException()

    return await _cached_read_dives(
        request,
        user_id=user_id,
        db=db,
        page=page,
        items_per_page=items_per_page,
        trip_id=trip_id,
        dive_site_id=dive_site_id,
    )


@cache(key_prefix="dive_cache", resource_id_name="id")
async def _cached_read_dive(request: Request, id: int, db: AsyncSession) -> DiveReadWithMixtures:
    """Fetches (and caches) a single dive by id, regardless of owner.

    Like `_cached_read_dives`, this must only be called after authorization has already
    been checked, since `@cache` can serve a cached response without re-checking it.
    """
    db_dive = await crud_dives.get(db=db, id=id, is_deleted=False, schema_to_select=DiveRead)
    if db_dive is None:
        raise NotFoundException("Dive not found")

    mixtures = await get_mixtures_for_dive(db=db, dive_id=id)
    dive_sites = await get_dive_sites_for_dive(db=db, dive_id=id)
    return DiveReadWithMixtures(**cast(dict[str, Any], db_dive), mixtures=mixtures, dive_sites=dive_sites)


@router.get("/dive/{id}", response_model=DiveReadWithMixtures)
async def read_dive(
    request: Request,
    id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveReadWithMixtures:
    db_dive = await crud_dives.get(db=db, id=id, is_deleted=False, schema_to_select=DiveRead)
    if db_dive is None:
        raise NotFoundException("Dive not found")

    if _dive_owner_id(db_dive) != current_user["id"]:
        raise ForbiddenException()

    return await _cached_read_dive(request, id=id, db=db)


@router.patch("/dive/{id}")
@cache("dive_cache", resource_id_name="id")
async def patch_dive(
    request: Request,
    id: int,
    values: DiveUpdateRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    db_dive = await crud_dives.get(db=db, id=id, is_deleted=False, schema_to_select=DiveRead)
    if db_dive is None:
        raise NotFoundException("Dive not found")

    owner_id = _dive_owner_id(db_dive)
    if owner_id != current_user["id"]:
        raise ForbiddenException()

    if values.trip_id is not None and not await trip_belongs_to_user(db=db, trip_id=values.trip_id, user_id=owner_id):
        raise HTTPException(status_code=422, detail="Trip not found.")

    if values.dive_site_ids is not None and not await dive_site_ids_belong_to_user(
        db=db, dive_site_ids=values.dive_site_ids, user_id=owner_id
    ):
        raise HTTPException(status_code=422, detail="Dive site not found.")

    update_data = values.model_dump(exclude={"mixtures", "dive_site_ids"}, exclude_unset=True)
    if update_data:
        try:
            await crud_dives.update(db=db, object=update_data, id=id)
        except IntegrityError as e:
            await db.rollback()
            raise HTTPException(status_code=422, detail=_fk_error_detail(e)) from e

    if values.mixtures is not None:
        try:
            await replace_mixtures_for_dive(db=db, dive_id=id, mixtures=values.mixtures)
        except IntegrityError as e:
            await db.rollback()
            raise HTTPException(status_code=422, detail=_mixture_error_detail(e)) from e

    if values.dive_site_ids is not None:
        try:
            await replace_dive_sites_for_dive(db=db, dive_id=id, dive_site_ids=values.dive_site_ids)
        except IntegrityError as e:
            await db.rollback()
            raise HTTPException(status_code=422, detail=_fk_error_detail(e)) from e

    if update_data or values.mixtures is not None or values.dive_site_ids is not None:
        await recalculate_dive_stats(db=db, user_id=owner_id)
        await delete_keys_by_pattern(f"user_{owner_id}_dives:*")

    return {"message": "Dive updated"}


@router.delete("/dive/{id}")
@cache("dive_cache", resource_id_name="id")
async def erase_dive(
    request: Request,
    id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    db_dive = await crud_dives.get(db=db, id=id, is_deleted=False, schema_to_select=DiveRead)
    if db_dive is None:
        raise NotFoundException("Dive not found")

    owner_id = _dive_owner_id(db_dive)
    if owner_id != current_user["id"]:
        raise ForbiddenException()

    await crud_dives.delete(db=db, id=id)
    await recalculate_dive_stats(db=db, user_id=owner_id)
    await delete_keys_by_pattern(f"user_{owner_id}_dives:*")

    return {"message": "Dive deleted"}
