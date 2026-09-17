import uuid as uuid_pkg
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query, Request
from fastcrud import PaginatedListResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import fetch_owned_or_raise, get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import (
    DuplicateValueException,
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
    DiveSiteSuggestion,
    DiveSiteSuggestResponse,
    DiveSiteUpdate,
)
from ...services.cache_invalidation import invalidate_dive_caches
from ...services.dive_site_catalog import search_sites

router = APIRouter(tags=["dive-sites"])


async def _get_owned_dive_site(db: AsyncSession, uuid: uuid_pkg.UUID, current_user: dict) -> DiveSiteReadInternal:
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

    Uniqueness is on name *and* location together, so the same site name at a different
    location is allowed; a genuine repeat is a 422. `latitude` and `longitude` are one
    value: send both or neither, since half a pair is a 422 as well.
    """
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

    `search` matches a case-insensitive substring against name and location, which is
    what backs the dive form's picker: it narrows server-side as you type rather than
    shipping the whole list to the browser. Out-of-range pagination is clamped, not
    rejected.
    """
    page, items_per_page = clamp_pagination(page, items_per_page)

    return await _dive_site_cache.read_list(
        request,
        user_id=current_user["id"],
        user_uuid=current_user["uuid"],
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
) -> dict[str, str]:
    """Delete a dive site, optionally moving the dives logged at it to another site.

    404 unless the caller owns it, exactly as for a site that doesn't exist - and a second
    `DELETE` on the same uuid is now a 404 too, because the row really is gone. This route
    used to be idempotent to insure against a half-failed multi-statement delete; one
    `DELETE FROM dive_site` in one transaction cannot half-fail.

    The `dive_dive_site` rows linking it to the dives logged there go with it
    (`ON DELETE CASCADE`) - the rule was already declared on the FK and finally fires - so
    those dives read back one site shorter and their cached reads are invalidated too.
    Nothing survives the delete; `move_dives_to` before it if the association matters.

    Pass `move_dives_to` and every one of the caller's live dives logged here has this site
    swapped for that one first, in the same transaction as the delete: either the whole log
    moved and this site is gone, or nothing happened. The replacement takes this site's slot
    in each dive's ordered site list - so it inherits being the primary site if this one was
    - and a dive already logged at both ends up holding it once. A replacement that isn't
    the caller's own site, or that is this site, is a 422: the same answer `PATCH /dive`
    gives for a `dive_site_uuids` entry it can't resolve, which is the per-dive call this
    parameter exists to replace.

    The response is the bare `{"message": ...}` every other delete on the API returns; the
    count of what moved is not reported. See DECISIONS.md.
    """
    db_dive_site = await _get_owned_dive_site(db, uuid, current_user)
    owner_id = db_dive_site.user_id

    if move_dives_to is not None:
        if move_dives_to == uuid:
            raise UnprocessableEntityException("A dive site cannot be moved onto itself.")
        site_id_by_uuid = await resolve_dive_site_ids_for_user(db=db, dive_site_uuids=[move_dives_to], user_id=owner_id)
        if site_id_by_uuid is None:
            raise UnprocessableEntityException("Dive site not found.")
        await replace_dive_site_on_dives(
            db=db,
            user_id=owner_id,
            from_dive_site_id=db_dive_site.id,
            to_dive_site_id=site_id_by_uuid[move_dives_to],
        )

    # Commits the reassignment above along with the delete - `crud_dive_sites.delete` is the
    # only writer here that commits, and both wrote through this one session.
    await crud_dive_sites.delete(db=db, uuid=uuid)
    await _dive_site_cache.invalidate_list(owner_id)
    # The cascade shortens the site list of every dive logged here, so drop those reads too.
    await invalidate_dive_caches(owner_id)

    return {"message": "Dive site deleted"}


# -------------- catalog suggestions --------------
# The one route in this module that is about nobody's dive sites. Everything above is scoped
# to the caller; this answers from a read-only catalog vendored in the image
# (`services/dive_site_catalog.py`), so two accounts asking the same thing get byte-identical
# answers. Picking a suggestion does not link to it - the client copies the values into an
# ordinary `POST /dive-site`, and nothing downstream knows the catalog exists.
#
# **No `@cache` decorator**, for the reason `api/v1/species.py` gives for its own search: the
# answer is local, non-user-scoped and immutable until the image is rebuilt, so a cache would
# mean building an invalidation surface for a problem that does not exist. Scanning a few
# thousand value objects already in memory is cheaper than the Redis round trip would be.
#
# **No per-user rate budget either**, unlike both comparable endpoints (`/species/search` and
# `/geocode/search`, 600/hour each), and the absence is argued rather than overlooked. Those
# budgets exist because each request spends a scarce *shared* resource - a third party's
# goodwill, a remote catalogue's capacity - and bound what one account can make this instance
# do to somebody else. This request spends a bounded scan over a file already resident in
# memory: no third party, no network, no database. That is the profile of `GET /dives`, which
# carries no budget either. If it ever stops being true, `enforce_rate_limit` is one awaited
# call away and fails open when Redis is absent.
#
# No route-ordering hazard, unlike species: every parameterised dive-site route lives under
# the singular `/dive-site/{uuid}`, so a literal subpath of the plural segment can be declared
# anywhere. This follows `dives.py`'s numbering block - a labelled section of literal plural
# subpaths at the end of the module.
@router.get("/dive-sites/suggest", response_model=DiveSiteSuggestResponse)
async def read_dive_site_suggestions(
    current_user: Annotated[dict, Depends(get_current_user)],
    q: Annotated[str, Query(min_length=2, max_length=200, description="Part of a dive site's name.")],
    latitude: Annotated[
        float | None,
        Query(ge=-90, le=90, description="Rank by distance from this position, when the form has one."),
    ] = None,
    longitude: Annotated[float | None, Query(ge=-180, le=180, description="Longitude of that position.")] = None,
) -> DiveSiteSuggestResponse:
    """Find named dive sites to prefill a new site from - "thistlegorm", "blue hole".

    Answers from a catalog of real dive sites extracted from OpenStreetMap and Wikidata and
    shipped inside this image, which is the gap the place search cannot fill: a geocoder
    knows where Dahab is, not where the Blue Hole's north entry is. Nothing is stored and
    nothing is owned - picking a suggestion means creating an ordinary dive site of your own
    from its values, which you can then rename, move and annotate like any other.

    Send `latitude` and `longitude` together when the form already has a position and results
    come back nearest first, which is the only thing that separates a same-name cluster:
    there are five `Shark Point`s in four countries. Send neither and they are ranked by how
    well the name matches. Sending one alone is a 422.

    Matching is case-insensitive and looks at both the site's local name and its English one,
    so 砂辺 is reachable by typing "Sunabe". `has_more` means the answer was cut by the
    result cap - keep typing rather than expecting the rest.

    `country` and `region` are English display names and either may be null; a site far
    enough offshore belongs to no administrative area at all. Every result carries the
    `attribution` its source's licence requires, and a client showing these must render it.

    Returns an empty list rather than an error when nothing matches, and also when the
    catalog file itself cannot be read - a diver can always type the site in by hand, and a
    500 here would make the form look broken over a convenience.
    """
    if (latitude is None) != (longitude is None):
        # The same rule `WholeCoordinatePair` enforces on the write schemas, and for the same
        # reason: half a pair is not a partial position but a meaningless one. Enforced here
        # rather than silently ignored, because a client that sent one and not the other has
        # a bug, and ranking by match quality while it believes it asked for proximity is the
        # kind of wrongness nobody notices.
        raise UnprocessableEntityException("latitude and longitude must be sent together")

    sites, has_more = search_sites(q.strip(), latitude, longitude)
    return DiveSiteSuggestResponse(
        results=[
            DiveSiteSuggestion(
                name=site.name,
                name_en=site.name_en,
                latitude=site.latitude,
                longitude=site.longitude,
                # `country_code` stays behind deliberately - see `DiveSiteSuggestion`.
                country=site.country,
                region=site.region,
                source=cast(Any, site.source),
                source_id=site.source_id,
                attribution=site.attribution,
            )
            for site in sites
        ],
        has_more=has_more,
    )
