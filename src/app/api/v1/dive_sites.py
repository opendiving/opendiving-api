import uuid as uuid_pkg
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query, Request
from fastcrud import PaginatedListResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import fetch_owned_or_raise, get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import (
    DuplicateValueException,
    ForbiddenException,
    NotFoundException,
    UnprocessableEntityException,
)
from ...core.utils.cache import cache
from ...core.utils.owned_resource_cache import OwnedResourceCache
from ...core.utils.pagination import clamp_pagination
from ...crud.crud_dive_dive_sites import replace_dive_site_on_dives
from ...crud.crud_dive_sites import crud_dive_sites, dive_site_name_exists, resolve_dive_site_ids_for_user
from ...schemas.dive_site import (
    DiveSiteCreate,
    DiveSiteCreateInternal,
    DiveSiteRead,
    DiveSiteReadInternal,
    DiveSiteUpdate,
)
from ...services.cache_invalidation import invalidate_dive_caches

router = APIRouter(tags=["dive-sites"])


async def _get_owned_dive_site(
    db: AsyncSession, uuid: uuid_pkg.UUID, current_user: dict, *, include_deleted: bool = False
) -> DiveSiteReadInternal:
    """Fetch a dive site by public uuid and assert the caller owns it.

    Thin wrapper over `fetch_owned_or_raise` - see there for why someone else's row reads
    as a 404 and, in particular, why this must run before any `@cache`-wrapped read
    helper.
    """
    return await fetch_owned_or_raise(
        db=db,
        crud=crud_dive_sites,
        uuid=uuid,
        current_user=current_user,
        schema=DiveSiteReadInternal,
        not_found_message="Dive site not found",
        include_deleted=include_deleted,
    )


def _to_public_dive_site(
    db_dive_site: DiveSiteReadInternal | dict[str, Any], *, user_uuid: uuid_pkg.UUID
) -> DiveSiteRead:
    """Convert an internal dive site representation (integer FKs) into its public shape
    (owning user referenced by `uuid`)."""
    data = db_dive_site if isinstance(db_dive_site, dict) else db_dive_site.model_dump()
    return DiveSiteRead(**{k: v for k, v in data.items() if k not in ("id", "user_id")}, user_uuid=user_uuid)


_dive_site_cache: OwnedResourceCache[DiveSiteReadInternal, DiveSiteRead] = OwnedResourceCache(
    resource_name="dive_sites",
    resource_label="Dive site",
    item_cache_prefix="dive_site_cache",
    crud=crud_dive_sites,
    schema_to_select=DiveSiteReadInternal,
    to_public=lambda db_dive_site, user_uuid: _to_public_dive_site(db_dive_site, user_uuid=user_uuid),
    sort_columns="name",
    sort_orders="asc",
    # A diver with hundreds of logged sites can't usefully scroll them, so the dive form's
    # picker narrows the list server-side as you type. Location is searched alongside the
    # name because that's how people remember sites they haven't dived in a while ("that
    # wall in Dahab") - see DECISIONS.md.
    search_columns=("name", "location"),
)


@router.post("/dive-site", response_model=DiveSiteRead, status_code=201)
async def write_dive_site(
    request: Request,
    dive_site: DiveSiteCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveSiteRead:
    """Create a dive site for the authenticated user.

    `user_uuid` in the body must be the caller's own (403 otherwise). Uniqueness is on
    name *and* location together, so the same site name at a different location is
    allowed; a genuine repeat is a 422. `latitude` and `longitude` are one value: send
    both or neither, since half a pair is a 422 as well.
    """
    if current_user["uuid"] != dive_site.user_uuid:
        raise ForbiddenException()

    if await dive_site_name_exists(db=db, user_id=current_user["id"], name=dive_site.name, location=dive_site.location):
        raise DuplicateValueException("A dive site with this name already exists at this location")

    dive_site_internal_dict = dive_site.model_dump(exclude={"user_uuid"})
    dive_site_internal = DiveSiteCreateInternal(**dive_site_internal_dict, user_id=current_user["id"])
    created_dive_site = await crud_dive_sites.create(
        db=db, object=dive_site_internal, schema_to_select=DiveSiteReadInternal, return_as_model=True
    )
    await _dive_site_cache.invalidate_list(current_user["id"])

    dive_site_read = await crud_dive_sites.get(
        db=db, id=created_dive_site.id, schema_to_select=DiveSiteReadInternal, return_as_model=True
    )
    if dive_site_read is None:
        raise NotFoundException("Created dive site not found")

    return _to_public_dive_site(cast(DiveSiteReadInternal, dive_site_read), user_uuid=current_user["uuid"])


@router.get("/dive-sites", response_model=PaginatedListResponse[DiveSiteRead])
async def read_dive_sites(
    request: Request,
    user_uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: int = 1,
    items_per_page: int = 10,
    search: Annotated[
        str | None,
        Query(max_length=255, description="Case-insensitive substring match on name or location"),
    ] = None,
) -> dict:
    """List the caller's dive sites.

    `user_uuid` must be the caller's own (403 otherwise). `search` matches a
    case-insensitive substring against name and location, which is what backs the dive
    form's picker: it narrows server-side as you type rather than shipping the whole list
    to the browser. Out-of-range pagination is clamped, not rejected.
    """
    if current_user["uuid"] != user_uuid:
        raise ForbiddenException()

    page, items_per_page = clamp_pagination(page, items_per_page)

    return await _dive_site_cache.read_list(
        request,
        user_id=current_user["id"],
        user_uuid=user_uuid,
        db=db,
        page=page,
        items_per_page=items_per_page,
        # Normalized here rather than in the cache layer so that " Blue " and "blue" share
        # one cache entry instead of two identical ones under different keys.
        search=(search or "").strip().lower() or None,
    )


@router.get("/dive-site/{uuid}", response_model=DiveSiteRead)
async def read_dive_site(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveSiteRead:
    """Return a single dive site by its public uuid.

    404 when no such site exists - and the same 404 when it belongs to another user, so
    someone else's uuid stays unprobeable.
    """
    # Authorize before the cached read: `@cache` replays a hit without re-checking.
    await _get_owned_dive_site(db, uuid, current_user)

    return await _dive_site_cache.read_item(request, uuid=uuid, owner_uuid=current_user["uuid"], db=db)


@router.patch("/dive-site/{uuid}")
@cache("dive_site_cache", resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)
async def patch_dive_site(
    request: Request,
    uuid: uuid_pkg.UUID,
    values: DiveSiteUpdate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Partially update a dive site; omitted fields are left untouched.

    404 unless the caller owns it, exactly as for a site that doesn't exist. Uniqueness
    is re-checked against the *resulting* name and location, so moving a site to a
    location where that name is already taken is a 422. `latitude` and `longitude` are
    one value: a body naming one without the other is a 422, so moving a site means
    sending both and clearing it means sending both as null. Because dive reads embed
    this site's name, location and position, a change to any of them also invalidates
    every cached dive logged here.
    """
    db_dive_site = await _get_owned_dive_site(db, uuid, current_user)

    # Two gates, not one, because the two questions have different answers. Uniqueness is
    # a rule about name-at-location and nothing else; staleness is about every field
    # `DiveSiteInfo` embeds, which now includes the position. Only `latitude` is tested
    # here: `WholeCoordinatePair` has already refused any body that names one coordinate
    # without the other, so longitude never travels alone.
    touches_name_or_location = values.name is not None or "location" in values.model_fields_set
    touches_dive_summary = touches_name_or_location or "latitude" in values.model_fields_set
    effective_name = values.name if values.name is not None else db_dive_site.name
    effective_location = values.location if "location" in values.model_fields_set else db_dive_site.location

    if touches_name_or_location and await dive_site_name_exists(
        db=db,
        user_id=db_dive_site.user_id,
        name=effective_name,
        location=effective_location,
        exclude_id=db_dive_site.id,
    ):
        raise DuplicateValueException("A dive site with this name already exists at this location")

    update_data = values.model_dump(exclude_unset=True)
    if update_data:
        await crud_dive_sites.update(db=db, object=update_data, uuid=uuid)
        await _dive_site_cache.invalidate_list(db_dive_site.user_id)
        # Dive reads embed this site's name, location and position, so a rename or a
        # dragged marker makes every cached dive logged here stale - the bug that used to
        # be documented as a known limitation, fixable now that the single-dive cache key
        # is user-scoped. Those three and nothing else: `DiveSiteInfo` carries no notes,
        # and dropping every cached dive a diver has because they retyped a description
        # would be a real cost for no staleness avoided.
        if touches_dive_summary:
            await invalidate_dive_caches(db_dive_site.user_id)

    return {"message": "Dive site updated"}


@router.delete("/dive-site/{uuid}")
@cache("dive_site_cache", resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)
async def erase_dive_site(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    move_dives_to: Annotated[
        uuid_pkg.UUID | None,
        Query(description="Move the dives logged at this site onto the site with this uuid before deleting it"),
    ] = None,
) -> dict[str, str | int]:
    """Soft-delete a dive site, optionally moving the dives logged at it to another site.

    404 unless the caller owns it, exactly as for a site that doesn't exist. Idempotent
    otherwise: deleting an already-deleted site succeeds rather than 404ing. The site
    stays attached to the dives logged at it, so their cached reads are invalidated too.

    Pass `move_dives_to` and every one of the caller's live dives logged here has this site
    swapped for that one first, in the same transaction as the delete: either the whole log
    moved and this site is gone, or nothing happened. The replacement takes this site's slot
    in each dive's ordered site list - so it inherits being the primary site if this one was
    - and a dive already logged at both ends up holding it once. A replacement that isn't
    the caller's own live site, or that is this site, is a 422: the same answer `PATCH
    /dive` gives for a `dive_site_uuids` entry it can't resolve, which is the per-dive call
    this parameter exists to replace.

    Passing it for an already-deleted site is honoured rather than refused - the dives are
    still there to move, and refusing would make the retry of a half-failed delete worse
    than the first attempt.

    `moved_dives` counts the dives that changed, for the "12 dives moved to Coral Garden"
    the web app says afterwards. It is present either way, and 0 when the parameter was
    omitted.
    """
    # `include_deleted`: deleting an already-soft-deleted site is a no-op, not a 404.
    db_dive_site = await _get_owned_dive_site(db, uuid, current_user, include_deleted=True)
    owner_id = db_dive_site.user_id

    moved_dives = 0
    if move_dives_to is not None:
        if move_dives_to == uuid:
            raise UnprocessableEntityException("A dive site cannot be moved onto itself.")
        site_id_by_uuid = await resolve_dive_site_ids_for_user(db=db, dive_site_uuids=[move_dives_to], user_id=owner_id)
        if site_id_by_uuid is None:
            raise UnprocessableEntityException("Dive site not found.")
        moved_dives = await replace_dive_site_on_dives(
            db=db,
            user_id=owner_id,
            from_dive_site_id=db_dive_site.id,
            to_dive_site_id=site_id_by_uuid[move_dives_to],
        )

    # Commits the reassignment above along with the delete - `crud_dive_sites.delete` is the
    # only writer here that commits, and both wrote through this one session.
    await crud_dive_sites.delete(db=db, uuid=uuid)
    await _dive_site_cache.invalidate_list(owner_id)
    # Soft-deleted sites stay on the dives logged at them, so drop those reads too.
    await invalidate_dive_caches(owner_id)

    return {"message": "Dive site deleted", "moved_dives": moved_dives}
