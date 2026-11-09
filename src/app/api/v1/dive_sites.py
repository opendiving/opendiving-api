import uuid as uuid_pkg
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy import func, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import fetch_owned_or_raise, get_current_user
from ...api.integrity import write_or_422
from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import (
    DuplicateValueException,
    ForbiddenException,
    NotFoundException,
    UnprocessableEntityException,
)
from ...core.utils.cache import cache
from ...core.utils.owned_resource_cache import OwnedResourceCache
from ...core.utils.pagination import ItemsPerPageQuery, PageQuery, clamp_pagination
from ...core.utils.rate_limit import enforce_rate_limit
from ...crud.crud_dive_dive_sites import replace_dive_site_on_dives
from ...crud.crud_dive_site_proposals import get_latest_proposals_for_sites
from ...crud.crud_dive_site_search import Box
from ...crud.crud_dive_sites import (
    DIVE_SITE_NAME_KEY,
    DIVE_SITE_SEARCH_COLUMNS,
    add_saved_site,
    aliased_records,
    crud_dive_sites,
    dive_site_name_exists,
    get_commons_matches_for_sites,
    get_dive_sites_page,
    get_readable_dive_site,
    get_summaries_for_dive_sites,
    get_unsaved_commons_page,
    in_the_commons,
    is_saved,
    remove_saved_commons_site,
    replace_saved_site_notes,
    replace_site_with_record,
    resolve_dive_site_ids_for_user,
    save_commons_site,
    save_named_commons_sites,
    search_condition,
)
from ...crud.crud_lookups import get_lookup_page
from ...crud.crud_tags import (
    get_tags_for_dive_sites,
    replace_tags_for_dive_site,
    resolve_tag_id_for_user,
    resolve_tag_ids,
)
from ...crud.unique_keys import write_or_duplicate
from ...models.dive_dive_site import DiveDiveSite
from ...models.dive_site import DiveSite
from ...models.saved_site import SavedSite
from ...schemas.dive_site import (
    DEPTH_RANGE_MESSAGE,
    SITE_OWNER_ACCOUNT,
    SITE_OWNER_COMMONS,
    DiveSiteCommonsMatch,
    DiveSiteCommonsMatchRead,
    DiveSiteCreate,
    DiveSiteCreateInternal,
    DiveSiteListSort,
    DiveSiteLookupItem,
    DiveSiteProposalState,
    DiveSiteRead,
    DiveSiteReadInternal,
    DiveSiteReplace,
    DiveSiteSave,
    DiveSiteSummary,
    DiveSiteUpdateRequest,
    canonical_other_names,
)
from ...schemas.dive_site_search import DiveSitePoints, DiveSiteSearchItem, DiveSiteSearchPage, PlaceSearchItem
from ...schemas.location import (
    DIVE_SITE_LOCATION_PREFIX,
    LOCATION_FIELDS,
    location_columns,
    location_from_row,
)
from ...schemas.lookup import LookupUntil, LookupUuids, lookup_bound
from ...schemas.tag import TAG_NOT_FOUND
from ...services.cache_invalidation import invalidate_dive_caches, invalidate_dive_site_caches, invalidate_trip_caches
from ...services.commons_matches import find_commons_matches
from ...services.commons_proposals import follow_the_edit, proposal_following_an_edit
from ...services.commons_records import attribution_for
from ...services.dive_site_search import SiteSearch, SiteSearchResult, search_dive_sites
from ..idempotency import IdempotentCreate

router = APIRouter(tags=["dive-sites"])

_NAME_TAKEN = "A dive site with this name already exists at this location"
_SHARED_MEMBER_REFUSED = "A Commons site's shared details cannot be changed here; only your notes and tags can"
_NOT_SAVED_REFUSED = "Save this Commons site to your sites before adding notes or tags"


async def _get_row_under_uuid(db: AsyncSession, uuid: uuid_pkg.UUID, current_user: dict) -> DiveSiteReadInternal:
    """Fetch the row carrying this public uuid and assert the caller may read it: their own, or
    any Commons record.

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
        unowned_is_shared=True,
    )


async def _get_readable_dive_site(db: AsyncSession, uuid: uuid_pkg.UUID, current_user: dict) -> DiveSiteReadInternal:
    """`_get_row_under_uuid`, where a uuid the caller replaced with a Commons record means the
    record. A caller given the record passes on its uuid, never the one in the path."""
    try:
        return await _get_row_under_uuid(db, uuid, current_user)
    except NotFoundException:
        aliased = (await aliased_records(db, [uuid], user_id=current_user["id"])).get(uuid)
        if aliased is None:
            raise
        return await _get_row_under_uuid(db, aliased.uuid, current_user)


async def _get_own_dive_site(db: AsyncSession, uuid: uuid_pkg.UUID, current_user: dict) -> DiveSiteReadInternal:
    """`_get_row_under_uuid` for a route that means the caller's own site alone."""
    return await fetch_owned_or_raise(
        db=db,
        crud=crud_dive_sites,
        uuid=uuid,
        current_user=current_user,
        schema=DiveSiteReadInternal,
        not_found_message="Dive site not found",
    )


def _relation(row: dict[str, Any]) -> dict[str, Any]:
    """Who owns a site and the credit it asks for, from its internal columns."""
    if row["user_id"] is not None:
        return {"owner": SITE_OWNER_ACCOUNT, "attribution": None}
    return {"owner": SITE_OWNER_COMMONS, "attribution": attribution_for(row["commons_source"])}


_INTERNAL_MEMBERS = {
    "id",
    "user_id",
    "commons_source",
    "commons_license",
    "commons_seed_registry",
    "commons_seed_identifier",
    "commons_retrieved",
    "commons_reviewed_at",
    *(f"{DIVE_SITE_LOCATION_PREFIX}{field}" for field in LOCATION_FIELDS),
}


def _to_public_dive_site(
    data: dict[str, Any],
    *,
    tags: list[str],
    summary: DiveSiteSummary,
    proposal: DiveSiteProposalState | None = None,
    commons_match: DiveSiteCommonsMatch | None = None,
) -> DiveSiteRead:
    """Convert an internal dive site representation (integer FKs, flat locality columns, the
    diver's notes beside them) into its public shape (who owns it, locality nested, tags and
    the summary of the diver's dives attached, with what the Commons has to say to this reader)."""
    return DiveSiteRead(
        **{k: v for k, v in data.items() if k not in _INTERNAL_MEMBERS},
        **summary.model_dump(),
        **_relation(data),
        location=location_from_row(data, DIVE_SITE_LOCATION_PREFIX),
        tags=tags,
        proposal=proposal,
        commons_match=commons_match,
    )


async def _commons_matches_of(
    db: AsyncSession, sites: list[dict[str, Any]], *, user_id: int
) -> dict[int, DiveSiteCommonsMatch]:
    """*commons_match* for the sites here that are this diver's own and carry an entry at all."""
    own = [site["id"] for site in sites if site["user_id"] == user_id and site["external_ids"]]
    return await get_commons_matches_for_sites(db, dive_site_ids=own)


# Kept for its list key's shape and its `lookup`, as `trips.py` keeps its own: a site read carries its tags and
# the summary of its dives, two further queries zipped back into the page, and the list sorts
# on that summary, which no `sort_columns` string can name. Both reads are hand-written below,
# and the single one sits under `user_{id}_dive_site` so that `invalidate_dive_site_caches`
# drops it by pattern with the list - which every dive write moving a summary has to do,
# naming no site.
_dive_site_cache: OwnedResourceCache[DiveSiteReadInternal, DiveSiteRead] = OwnedResourceCache(
    resource_name="dive_sites",
    crud=crud_dive_sites,
    search_columns=DIVE_SITE_SEARCH_COLUMNS,
)


async def _resolved_tag_ids(db: AsyncSession, *, user_id: int, names: list[str]) -> list[int]:
    return await resolve_tag_ids(db, user_id=user_id, names=names) if names else []


@router.post(
    "/dive-site",
    response_model=DiveSiteRead,
    status_code=201,
    dependencies=[Depends(IdempotentCreate(DiveSite, read="/dive-site/{uuid}"))],
)
async def write_dive_site(
    request: Request,
    dive_site: DiveSiteCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveSiteRead:
    """Create a dive site for the authenticated user, answering 201 with the site as
    `GET /dive-site/{uuid}` reads it.

    Uniqueness is on the site's name *and* its locality's name together, so the same site
    name in a different place is allowed; a genuine repeat is a 422. `latitude` and
    `longitude` are one value: send both or neither, since half a pair is a 422 as well.

    `location` is the whole place - the name it is known by, the locality's own centre and
    its extent - and every member but the name is optional, so a locality the diver typed
    is a name and nothing else. Its position is the *place's*, never the site's: filling it
    from the site's own pin would claim the town sits exactly where the marker was dropped.

    `other_names`, `external_ids` and `entry_types` are stored conforming, not refused:
    another name the name or an earlier one already says is dropped, compared trimmed and
    case-folded, and a repeated registry entry or entry type is kept once. An `external_ids`
    entry under a registry the format names - `wikidata`, `openstreetmap`, `worms` - must
    have that registry's identifier form; any other registry is stored as sent. `depth_from`
    above `depth_to` is a 422. `tags` are by name, each matched to the diver's tag of that
    name or created, as a dive's are.
    """
    location = dive_site.location
    if await dive_site_name_exists(
        db=db,
        user_id=current_user["id"],
        name=dive_site.name,
        location_name=None if location is None else location.name,
    ):
        raise DuplicateValueException(_NAME_TAKEN)

    tag_ids = await _resolved_tag_ids(db, user_id=current_user["id"], names=dive_site.tags)
    dive_site_internal = DiveSiteCreateInternal(
        **dive_site.model_dump(exclude={"location", "notes", "tags"}),
        **location_columns(location, DIVE_SITE_LOCATION_PREFIX),
        user_id=current_user["id"],
    )
    created_dive_site = await write_or_duplicate(
        db,
        crud_dive_sites.create(
            db=db, object=dive_site_internal, commit=False, schema_to_select=DiveSiteReadInternal, return_as_model=True
        ),
        DIVE_SITE_NAME_KEY,
        _NAME_TAKEN,
    )
    add_saved_site(
        db,
        user_id=current_user["id"],
        dive_site_id=created_dive_site.id,
        notes=dive_site.notes,
        created_at=created_dive_site.created_at,
    )
    if tag_ids:
        await write_or_422(
            db,
            replace_tags_for_dive_site(db, created_dive_site.id, tag_ids, user_id=current_user["id"], commit=False),
            TAG_NOT_FOUND,
        )
    # The new tags `_resolved_tag_ids` created commit with the site, or roll back with it.
    await db.commit()
    await invalidate_dive_site_caches(current_user["id"])

    tags = (
        (await get_tags_for_dive_sites(db, [created_dive_site.id], user_id=current_user["id"]))[created_dive_site.id]
        if tag_ids
        else []
    )
    created = created_dive_site.model_dump()
    matches = await _commons_matches_of(db, [created], user_id=current_user["id"])
    # No dive can name a site that did not exist, so a new one has nothing to summarise.
    return _to_public_dive_site(
        {**created, "notes": dive_site.notes, "saved": True},
        tags=tags,
        summary=DiveSiteSummary(),
        commons_match=matches.get(created_dive_site.id),
    )


# Every query parameter has to be in this key: `@cache` keys on its placeholders alone, so a
# filter or an order missing from it serves one page for another for sixty seconds. The shape
# up to the search is `_dive_site_cache`'s, which keeps it under the pattern
# `invalidate_dive_site_caches` sweeps.
_LIST_CACHE_KEY_PREFIX = _dive_site_cache.list_cache_key_prefix + ":tag_{tag_id}:sort_{sort}:match_{commons_match}"


@cache(key_prefix=_LIST_CACHE_KEY_PREFIX, resource_id_name="user_id", expiration=60)
async def _cached_read_dive_sites(
    request: Request,
    user_id: int,
    db: AsyncSession,
    page: int,
    items_per_page: int,
    search: str | None,
    tag_id: int | None,
    sort: DiveSiteListSort,
    commons_match: bool,
) -> dict:
    """Fetches (and caches) a page of the caller's sites, each with its tags and the summary
    of its dives. Only ever called once `read_dive_sites` has authorized the caller - a hit
    skips this body, authorization included."""
    sites_data = await get_dive_sites_page(
        db,
        user_id=user_id,
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
        search=search,
        tag_id=tag_id,
        sort=sort,
        commons_match=commons_match,
    )
    site_ids = [site["id"] for site in sites_data["data"]]
    tags_by_site = await get_tags_for_dive_sites(db, site_ids, user_id=user_id)
    summaries = await get_summaries_for_dive_sites(db, dive_site_ids=site_ids, user_id=user_id)
    proposals = await get_latest_proposals_for_sites(db, user_id=user_id, dive_site_ids=site_ids)
    matches = await _commons_matches_of(db, sites_data["data"], user_id=user_id)
    sites_data["data"] = [
        _to_public_dive_site(
            site,
            tags=tags_by_site[site["id"]],
            summary=summaries[site["id"]],
            proposal=proposals.get(site["id"]),
            commons_match=matches.get(site["id"]),
        ).model_dump()
        for site in sites_data["data"]
    ]
    response: dict[str, Any] = paginated_response(crud_data=sites_data, page=page, items_per_page=items_per_page)
    return response


@router.get("/dive-sites", response_model=PaginatedListResponse[DiveSiteRead])
async def read_dive_sites(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: PageQuery = 1,
    items_per_page: ItemsPerPageQuery = 10,
    search: Annotated[
        str | None,
        Query(max_length=255, description="Case-insensitive substring match on the name, other names or locality"),
    ] = None,
    tag_uuid: Annotated[uuid_pkg.UUID | None, Query(description="Only the sites carrying this tag")] = None,
    sort: Annotated[
        DiveSiteListSort,
        Query(
            description="`name`; `dive_count`, most dived first; or `last_dived_on`, most recently dived first and "
            "every site no dive names last. Ties go by name"
        ),
    ] = DiveSiteListSort.NAME,
    commons_match: Annotated[
        bool, Query(description="Only the caller's own sites that share a registry entry with a Commons record")
    ] = False,
) -> dict:
    """List the sites on the caller's list, their own and the Commons records they have saved,
    each with its tags and the summary of the caller's dives there.

    `search` matches a case-insensitive substring against the site's name, its other names
    and its locality's name. `tag_uuid` keeps the sites carrying that tag, and one that is
    not the caller's returns an empty page rather than an error. `commons_match` keeps the
    caller's own sites with a `commons_match`, so `total_count` is how many they have.
    Out-of-range pagination is clamped, not rejected.

    The summary counts the live dives naming the site at any position, so the second site of
    a drift dive counts that dive too.
    """
    page, items_per_page = clamp_pagination(page, items_per_page)

    tag_id: int | None = None
    if tag_uuid is not None:
        # -1 can never match a tag, so a foreign or unknown uuid answers an empty page.
        tag_id = await resolve_tag_id_for_user(db, tag_uuid=tag_uuid, user_id=current_user["id"]) or -1

    return await _cached_read_dive_sites(
        request,
        user_id=current_user["id"],
        db=db,
        page=page,
        items_per_page=items_per_page,
        # Normalized here rather than in the cache layer so that " Blue " and "blue" share
        # one cache entry instead of two identical ones under different keys.
        search=(search or "").strip().lower() or None,
        tag_id=tag_id,
        sort=sort,
        commons_match=commons_match,
    )


_LOOKUP_COLUMNS = (
    DiveSite.uuid,
    DiveSite.user_id,
    DiveSite.commons_source,
    DiveSite.name,
    DiveSite.other_names,
    DiveSite.latitude,
    DiveSite.longitude,
    *(getattr(DiveSite, f"{DIVE_SITE_LOCATION_PREFIX}{field}") for field in LOCATION_FIELDS),
    DiveSite.external_ids,
    DiveSite.water_type,
    DiveSite.altitude,
    DiveSite.entry_types,
)


def _lookup_item(row: dict[str, Any], *, saved: bool) -> dict[str, Any]:
    return DiveSiteLookupItem(
        uuid=row["uuid"],
        name=row["name"],
        other_names=row["other_names"],
        latitude=row["latitude"],
        longitude=row["longitude"],
        location=location_from_row(row, DIVE_SITE_LOCATION_PREFIX),
        external_ids=row["external_ids"],
        water_type=row["water_type"],
        altitude=row["altitude"],
        entry_types=row["entry_types"],
        saved=saved,
        **_relation(row),
    ).model_dump()


async def _lookup_dive_sites(
    *,
    user_id: int,
    db: AsyncSession,
    page: int,
    items_per_page: int,
    search: str | None,
    until: datetime | None,
    uuids: list[uuid_pkg.UUID] | None,
    latitude: float | None = None,
    longitude: float | None = None,
    commons_only: bool = False,
) -> dict:
    """A page of `GET /dive-sites/lookup`. Reached only after the route has authorized the
    caller. A search reads two groups one after the other, so a page may straddle them: the
    second answers for what the first left of the page. `commons_only` skips the first, whose
    locality matches can otherwise fill the page."""
    if uuids:
        aliased = await aliased_records(db, uuids, user_id=user_id)
        uuids = [aliased[uuid].uuid if uuid in aliased else uuid for uuid in uuids]
    offset, limit = compute_offset(page, items_per_page), items_per_page
    items: list[dict[str, Any]] = []
    total_count = 0
    if not commons_only:
        on_the_list = SavedSite.id.is_not(None)
        # Without a search the feed holds what is on the list, and a client caches every page of
        # it: only a form holding a Commons record by uuid reaches an unsaved one here.
        conditions: tuple[Any, ...] = (on_the_list if search or not uuids else on_the_list | in_the_commons(),)
        if search:
            conditions += (search_condition(search),)
        if uuids:
            conditions += (DiveSite.uuid.in_(uuids),)
        data = await get_lookup_page(
            db,
            model=DiveSite,
            columns=(*_LOOKUP_COLUMNS, on_the_list.label("saved")),
            conditions=conditions,
            used_by=DiveDiveSite.dive_site_id,
            user_id=user_id,
            bound=until,
            offset=offset,
            limit=limit,
            from_clause=DiveSite.__table__.outerjoin(
                SavedSite.__table__, (SavedSite.dive_site_id == DiveSite.id) & (SavedSite.user_id == user_id)
            ),
            created_at=func.coalesce(SavedSite.created_at, DiveSite.created_at),
        )
        items = [_lookup_item(row, saved=row["saved"]) for row in data["data"]]
        total_count = data["total_count"]
    if search:
        commons = await get_unsaved_commons_page(
            db,
            columns=_LOOKUP_COLUMNS,
            user_id=user_id,
            search=search,
            uuids=uuids,
            position=None if latitude is None or longitude is None else (latitude, longitude),
            offset=max(0, offset - total_count),
            limit=limit - len(items),
        )
        items += [_lookup_item(row, saved=False) for row in commons["data"]]
        total_count += commons["total_count"]
    response: dict[str, Any] = paginated_response(
        crud_data={"data": items, "total_count": total_count}, page=page, items_per_page=items_per_page
    )
    return response


# The order depends on the position and the rows on `commons_only`, so both are part of the key. A
# Commons change reaches a cached page at its 60-second expiry, since nothing sweeps every account's
# lookups for one.
_lookup = _dive_site_cache.lookup(_lookup_dive_sites, also_keyed_on=("latitude", "longitude", "commons_only"))


@router.get("/dive-sites/lookup", response_model=PaginatedListResponse[DiveSiteLookupItem])
async def lookup_dive_sites(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    until: LookupUntil = None,
    page: PageQuery = 1,
    items_per_page: ItemsPerPageQuery = 10,
    search: Annotated[
        str | None,
        Query(
            max_length=255,
            description="Case-insensitive substring match on the name, other names or locality of the caller's "
            "sites, and on the name or other names of the Commons records that follow them",
        ),
    ] = None,
    uuid: LookupUuids = None,
    latitude: Annotated[
        float | None,
        Query(ge=-90, le=90, description="With `search`, the Commons records nearest this position come first"),
    ] = None,
    longitude: Annotated[float | None, Query(ge=-180, le=180, description="Longitude of that position")] = None,
    commons_only: Annotated[
        bool, Query(description="With `search`, answer only the Commons records the caller has not saved")
    ] = False,
) -> dict:
    """The sites a picker lists: uuid, name, other names, pin, locality and registry entries,
    with the water type, altitude and entry types a dive form fills from them, who owns each and
    whether it is on the caller's list, and for a Commons record the credit to show beside it.

    Without `search`, the caller's list: the site a dive at or before `until` named most
    recently first, then sites no such dive names, newest saved first.

    With `search`, those that match in that order, then the Commons records the caller has not
    saved whose name or other names match: exact names before prefixes before substrings, by
    name within each, or nearest first where `latitude` and `longitude` are sent. One response,
    to render in the order it comes. The position is sent whole: one coordinate alone is a 422.

    `commons_only=true` with `search` answers those Commons records alone, in the same order.
    Without `search` it is a 422.

    `uuid` narrows the rows to the sites a form already holds: their own, saved, or any Commons
    record, saved or not. Out-of-range pagination is clamped. A Commons record's change may take
    up to a minute to show here.
    """
    if (latitude is None) != (longitude is None):
        raise UnprocessableEntityException("latitude and longitude must be sent together")
    # Only a search orders by the position, so any other request keeps one key whatever it sent.
    near = bool((search or "").strip())
    if commons_only and not near:
        raise UnprocessableEntityException("commons_only needs a search")
    page, items_per_page = clamp_pagination(page, items_per_page)
    return await _lookup(
        request,
        user_id=current_user["id"],
        db=db,
        page=page,
        items_per_page=items_per_page,
        search=search,
        until=until,
        uuids=uuid,
        latitude=latitude if near else None,
        longitude=longitude if near else None,
        commons_only=commons_only,
    )


def _search_item(
    row: dict[str, Any], *, summary: DiveSiteSummary | None, proposal: DiveSiteProposalState | None
) -> DiveSiteSearchItem:
    return DiveSiteSearchItem(
        **_lookup_item(row, saved=row["saved"]),
        kind="site",
        distance_m=row["distance_m"],
        match=row["match"],
        depth_from=row["depth_from"],
        depth_to=row["depth_to"],
        dives=summary,
        proposal=proposal,
    )


def _search_rows(
    result: SiteSearchResult,
    *,
    summaries: dict[int, DiveSiteSummary],
    proposals: dict[int, DiveSiteProposalState],
) -> list[DiveSiteSearchItem | PlaceSearchItem]:
    """The page's rows in order: the places go where the result places them among the sites."""
    sites: list[DiveSiteSearchItem | PlaceSearchItem] = [
        _search_item(row, summary=summaries.get(row["id"]), proposal=proposals.get(row["id"])) for row in result.sites
    ]
    places = [
        PlaceSearchItem(
            **found.place.model_dump(), kind="place", distance_m=found.distance_m, sites_nearby=found.sites_nearby
        )
        for found in result.places
    ]
    return [*sites[: result.places_at], *places, *sites[result.places_at :]]


@router.get("/dive-sites/search", response_model=DiveSiteSearchPage)
async def search_dive_sites_and_places(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    q: Annotated[
        str | None,
        Query(max_length=255, description="The text typed: a site's name or other name, or a place's name"),
    ] = None,
    latitude: Annotated[
        float | None, Query(ge=-90, le=90, description="Latitude of the point distances are measured from")
    ] = None,
    longitude: Annotated[float | None, Query(ge=-180, le=180, description="Longitude of that point")] = None,
    south: Annotated[float | None, Query(ge=-90, le=90, description="The view's southern edge")] = None,
    north: Annotated[float | None, Query(ge=-90, le=90, description="The view's northern edge")] = None,
    west: Annotated[
        float | None, Query(ge=-180, le=180, description="The view's western edge; above `east`, the view crosses 180°")
    ] = None,
    east: Annotated[float | None, Query(ge=-180, le=180, description="The view's eastern edge")] = None,
    until: LookupUntil = None,
    my: Annotated[bool, Query(description="Only the caller's own sites and the Commons records they saved")] = False,
    tag_uuid: Annotated[
        uuid_pkg.UUID | None, Query(description="With `my`, only the sites carrying this tag of the caller's")
    ] = None,
    commons_match: Annotated[
        bool | None,
        Query(description="With `my`, only the caller's own sites sharing a registry entry with a Commons record"),
    ] = None,
    sort: Annotated[
        DiveSiteListSort | None,
        Query(
            description="With `my`, the site rows in this order instead: `name`, `dive_count` or `last_dived_on`, "
            "as `GET /dive-sites` sorts"
        ),
    ] = None,
    page: PageQuery = 1,
    items_per_page: ItemsPerPageQuery = 10,
) -> DiveSiteSearchPage:
    """Find dive sites and places for a map and a list: one page of rows, sites and places in one
    order, to render as they come.

    With `q`, in tiers. First the caller's own and saved sites whose name or other name matches,
    then the Commons records they have not saved whose name or other name matches; within each an
    exact name before a name starting with `q` before one containing it, and nearer the point
    before farther. Then up to five places named `q`, nearest the point first and those with no
    site near them last, each with how many sites the caller can see inside its box, or within 25
    km of it where it has none. Then the sites that match only allowing for a typo, the closest
    resemblance first. Case and accents never matter. A site row's `match` says how it matched.

    The places sit after the tiers before them, on the page where those end, and appear on no
    other page; `has_more` counts site rows alone. Places come from a geocoder, so an answer may
    have none: a `q` under two characters, a geocoder that is off, slow or unreachable, and an
    account past its place-search budget all leave them out, and so does a slow geocoder, which
    costs only the places. A `q` under two characters is matched only as written, with no typo
    tier.

    Without `q`, with the point: the sites nearest it that have a pin, the caller's and the
    Commons alike, closest first. Without `q` or the point: the caller's own and saved sites,
    pinned or not, the one a dive at or before `until` named most recently first, then those no
    such dive names, newest saved first.

    The point is `latitude` and `longitude` together, and the view `south`, `north`, `west` and
    `east` together; half of either is a 422, as is `south` above `north`. A `west` above `east`
    crosses the antimeridian. The view narrows the site rows to those inside it, with `q` and
    without, and is ignored where neither `q`, the point nor `my=true` is sent.

    `my=true` narrows every row to the caller's own and saved sites, and a `q` then matches
    the locality's name as well, those found by their locality alone listed after the rest of the
    first tier with no `match`. Without `q` it answers every one of them, pinned or not, most
    recently dived first, or with the view only the pinned ones inside it, in the same order.
    With `my=true`, `tag_uuid` keeps the sites carrying that tag - one that is not the
    caller's answers an empty page - `commons_match` the caller's own sites sharing a registry
    entry with a Commons record, and `sort` orders every site row as `GET /dive-sites` does, the
    places following them. Any of the three without `my=true` is a 422. `total_count` is the
    number of site rows, with `my` alone.

    Each site row carries the summary of the caller's dives there in `dives`, null on a Commons
    record they have not saved, and their latest proposal on it in `proposal`. Not cached.
    Rate-limited per account; a request over it is a 429. Out-of-range pagination is clamped.
    """
    # Every coordinate is the client's to round before it is sent: the server keeps none, but
    # its access logs hold the query string.
    if (latitude is None) != (longitude is None):
        raise UnprocessableEntityException("latitude and longitude must be sent together")
    edges = (south, north, west, east)
    if any(edge is not None for edge in edges) and not all(edge is not None for edge in edges):
        raise UnprocessableEntityException("south, north, west and east must be sent together")
    if south is not None and north is not None and south > north:
        raise UnprocessableEntityException("south must not be above north")
    if not my and (tag_uuid is not None or commons_match is not None or sort is not None):
        raise UnprocessableEntityException("tag_uuid, commons_match and sort need my=true")
    await enforce_rate_limit(
        f"dive-site-search:user:{current_user['id']}",
        settings.DIVE_SITE_SEARCH_RATE_LIMIT_PER_USER,
        settings.DIVE_SITE_SEARCH_RATE_LIMIT_WINDOW_SECONDS,
    )
    page, items_per_page = clamp_pagination(page, items_per_page)
    user_id = current_user["id"]

    tag_id: int | None = None
    if tag_uuid is not None:
        # -1 can never match a tag, so a foreign or unknown uuid answers an empty page.
        tag_id = await resolve_tag_id_for_user(db, tag_uuid=tag_uuid, user_id=user_id) or -1
    camera = None if latitude is None or longitude is None else (latitude, longitude)
    box = None if south is None or north is None or west is None or east is None else Box(south, north, west, east)
    result = await search_dive_sites(
        db,
        user_id=user_id,
        search=SiteSearch(
            term=(q or "").strip() or None,
            camera=camera,
            box=box,
            my=my,
            until=lookup_bound(until),
            tag_id=tag_id,
            commons_match=bool(commons_match),
            sort=sort,
            page=page,
            items_per_page=items_per_page,
        ),
    )
    saved_ids = [row["id"] for row in result.sites if row["saved"]]
    summaries = await get_summaries_for_dive_sites(db, dive_site_ids=saved_ids, user_id=user_id)
    proposals = await get_latest_proposals_for_sites(db, user_id=user_id, dive_site_ids=saved_ids)
    return DiveSiteSearchPage(
        rows=_search_rows(result, summaries=summaries, proposals=proposals),
        has_more=result.has_more,
        total_count=result.total_count if my else None,
        page=page,
        items_per_page=items_per_page,
    )


# Under the single site's pattern, so every sweep of the caller's sites - a save, a removal, a
# review of a record they saved, a proposal of theirs made, withdrawn or decided - drops it. A
# sync's change to a record reaches it at the expiry.
@cache(key_prefix="user_{user_id}_dive_site:points", resource_id_name="user_id", expiration=60)
async def _cached_read_dive_site_points(request: Request, user_id: int, db: AsyncSession) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(DiveSite.id, DiveSite.longitude, DiveSite.latitude, DiveSite.uuid, DiveSite.name, DiveSite.user_id)
            .join(SavedSite, (SavedSite.dive_site_id == DiveSite.id) & (SavedSite.user_id == user_id))
            .where(DiveSite.latitude.is_not(None), DiveSite.longitude.is_not(None))
            .order_by(DiveSite.uuid)
        )
    ).all()
    proposals = await get_latest_proposals_for_sites(db, user_id=user_id, dive_site_ids=[row.id for row in rows])
    return DiveSitePoints(
        points=[
            (
                row.longitude,
                row.latitude,
                row.uuid,
                row.name,
                SITE_OWNER_COMMONS if row.user_id is None else SITE_OWNER_ACCOUNT,
                proposal.status if (proposal := proposals.get(row.id)) else None,
            )
            for row in rows
        ]
    ).model_dump()


@router.get("/dive-sites/points", response_model=DiveSitePoints)
async def read_dive_site_points(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    """Every site on the caller's list that has a pin - their own and the Commons records they
    saved - as `[longitude, latitude, uuid, name, owner, proposal]`, for a map to draw. `proposal`
    is the status of the caller's latest proposal on the site while it waits or once declined
    (`"waiting"` or `"declined"`), and null otherwise. A site with no pin is left out. A change to a
    saved Commons record may take up to a minute to show here.
    """
    # The caller's own rows alone, read under their id: nothing here needs authorizing first.
    return await _cached_read_dive_site_points(request, user_id=current_user["id"], db=db)


@cache(key_prefix="user_{user_id}_dive_site", resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)
async def _cached_read_dive_site(
    request: Request, user_id: int, uuid: uuid_pkg.UUID, db: AsyncSession
) -> dict[str, Any]:
    """Fetches (and caches) one site with this reader's tags and summary. Like the list, reached
    only once the route has established that the caller may read it."""
    db_dive_site = await get_readable_dive_site(db, uuid=uuid, user_id=user_id)
    if db_dive_site is None:
        raise NotFoundException("Dive site not found")

    site_id = db_dive_site["id"]
    tags = (await get_tags_for_dive_sites(db, [site_id], user_id=user_id))[site_id]
    summary = (await get_summaries_for_dive_sites(db, dive_site_ids=[site_id], user_id=user_id))[site_id]
    proposals = await get_latest_proposals_for_sites(db, user_id=user_id, dive_site_ids=[site_id])
    matches = await _commons_matches_of(db, [db_dive_site], user_id=user_id)
    return _to_public_dive_site(
        db_dive_site,
        tags=tags,
        summary=summary,
        proposal=proposals.get(site_id),
        commons_match=matches.get(site_id),
    ).model_dump()


@router.get("/dive-site/{uuid}", response_model=DiveSiteRead)
async def read_dive_site(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    """Return a single dive site by its public uuid, with the caller's tags and the summary of
    the caller's dives there that `GET /dive-sites` carries.

    The caller's own site, or any Commons record whether they have saved it or not. On one they
    have not saved, `saved` is false, `notes` and `tags` are empty, the summary is zero and
    `created_at` is when the record became a Commons record on this instance.

    `proposal` is the caller's latest proposal on the site while it waits or after a decline, and
    `commons_match`, on the caller's own site, the Commons record sharing a registry entry with
    it. Either may trail a change made elsewhere by the read's cache.

    The uuid of a site the caller replaced with a Commons record reads as that record, its `uuid`
    the record's, here and wherever the caller reads, saves or edits a site or names one on a dive.
    Deleting, replacing or proposing by that uuid is a 404.

    404 when no such site exists - and the same 404 when it is another user's private site, so
    someone else's uuid stays unprobeable.
    """
    # Authorize before the cached read: `@cache` replays a hit without re-checking. Keyed on the
    # row's uuid, so a replaced site's read is dropped by the sweeps that drop its record's.
    site = await _get_readable_dive_site(db, uuid, current_user)

    return await _cached_read_dive_site(request, user_id=current_user["id"], uuid=site.uuid, db=db)


@router.get("/dive-site/{uuid}/commons-matches", response_model=list[DiveSiteCommonsMatchRead])
async def read_dive_site_commons_matches(
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> list[DiveSiteCommonsMatchRead]:
    """The Commons records the caller's own site may be, strongest first and nearest first within
    a rule: `identity`, which shares a registry entry with the site; `within_20_m`;
    `similar_name_within_200_m`; and `same_name_within_5_km`, for a name that is not a generic one.

    `distance_metres` is between the two pins, null when the site has none. A site with no pin
    can only meet `identity`. `qualifier_siblings` says the names differ only by a word like West
    or East, which usually means two sites.

    404 when no such site exists, and the same 404 on a Commons record and on another user's
    private site.
    """
    site = await _get_own_dive_site(db, uuid, current_user)
    return await find_commons_matches(db, site.model_dump())


@router.post("/dive-site/{uuid}/replace", response_model=DiveSiteRead)
async def replace_dive_site(
    request: Request,
    uuid: uuid_pkg.UUID,
    values: DiveSiteReplace,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    """Replace the caller's own site with a Commons record, answering the record as
    `GET /dive-site/{uuid}` reads it.

    In one transaction: every one of the caller's live dives naming the site names the record at
    the same position, and a dive already naming both holds it once; the site's notes join any the
    caller had on the record; its tags are added to theirs there; the record is on their list; and
    the site is deleted, with any proposal on it. A draft naming the site names the record.

    The site's uuid goes on meaning the record to the caller alone: a dive, a read or an edit of
    the record's notes and tags that names it lands on the record, and to anyone else it names
    nothing.

    A `commons_site_uuid` that is not a Commons record is a 422. 404 when no such site exists,
    and the same 404 on a Commons record and on another user's private site, so a second call on
    the same uuid is a 404 too.
    """
    user_id = current_user["id"]
    site = await _get_own_dive_site(db, uuid, current_user)
    record_id = await db.scalar(select(DiveSite.id).where(DiveSite.uuid == values.commons_site_uuid, in_the_commons()))
    if record_id is None:
        raise UnprocessableEntityException("That Commons site does not exist")

    await replace_site_with_record(db, user_id=user_id, site_id=site.id, record_id=record_id)
    await db.commit()
    await invalidate_dive_site_caches(user_id)
    await invalidate_dive_caches(user_id)
    await invalidate_trip_caches(user_id)

    return await _cached_read_dive_site(request, user_id=user_id, uuid=values.commons_site_uuid, db=db)


@router.put("/dive-site/{uuid}/saved", response_model=DiveSiteRead)
async def save_dive_site(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    values: Annotated[DiveSiteSave | None, Body()] = None,
) -> dict[str, Any]:
    """Save a Commons record to the caller's list, with the notes and tags the optional body
    carries, answering the site as `GET /dive-site/{uuid}` reads it.

    Idempotent. On a site already on the list - saved before, or the caller's own - nothing
    changes, the body included, so a retry cannot overwrite what the caller wrote since. Change
    notes and tags on a saved record with `PATCH /dive-site/{uuid}`.

    `tags` are by name, each matched to the diver's tag of that name or created. 404 when no such
    site exists, and the same 404 for another user's private site.
    """
    user_id = current_user["id"]
    db_dive_site = await _get_readable_dive_site(db, uuid, current_user)
    notes, tag_names = ("", []) if values is None else (values.notes, values.tags)

    if db_dive_site.user_id is None and await save_commons_site(
        db, user_id=user_id, dive_site_id=db_dive_site.id, notes=notes
    ):
        # After the save, so a retry that changes nothing makes no tag either.
        tag_ids = await _resolved_tag_ids(db, user_id=user_id, names=tag_names)
        if tag_ids:
            await write_or_422(
                db,
                replace_tags_for_dive_site(db, db_dive_site.id, tag_ids, user_id=user_id, commit=False),
                TAG_NOT_FOUND,
            )
        await db.commit()
        await invalidate_dive_site_caches(user_id)

    return await _cached_read_dive_site(request, user_id=user_id, uuid=db_dive_site.uuid, db=db)


async def _patch_saved_commons_site(
    db: AsyncSession, site: DiveSiteReadInternal, values: DiveSiteUpdateRequest, *, user_id: int
) -> None:
    """The diver's own layer on a Commons record, the one part of it a `PATCH` can reach."""
    # Before either write, and before a tag is made: no tag ever sits on a site off the list.
    if not await is_saved(db, user_id=user_id, dive_site_id=site.id):
        raise HTTPException(status_code=409, detail=_NOT_SAVED_REFUSED)
    if values.model_fields_set - {"notes", "tags"}:
        raise ForbiddenException(_SHARED_MEMBER_REFUSED)

    tag_ids = None if values.tags is None else await _resolved_tag_ids(db, user_id=user_id, names=values.tags)
    if tag_ids is not None:
        await write_or_422(
            db, replace_tags_for_dive_site(db, site.id, tag_ids, user_id=user_id, commit=False), TAG_NOT_FOUND
        )
    if values.notes is not None:
        await replace_saved_site_notes(db, user_id=user_id, dive_site_id=site.id, notes=values.notes)
    if tag_ids is not None or values.notes is not None:
        await db.commit()
        await invalidate_dive_site_caches(user_id)


@router.patch("/dive-site/{uuid}")
async def patch_dive_site(
    request: Request,
    uuid: uuid_pkg.UUID,
    values: DiveSiteUpdateRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Partially update a dive site; omitted fields are left untouched.

    On a Commons record the caller has saved, `notes` and `tags` alone can be sent, and they are
    the caller's: a body naming any other member is a 403 and writes nothing. On a Commons record
    the caller has not saved, every body is a 409 and writes nothing: `PUT /dive-site/{uuid}/saved`
    saves it first, and can carry both.

    The uuid of a site the caller replaced with a Commons record is a `PATCH` on that record, so
    `notes` overwrite theirs on it and `tags` replace them.

    Everything below is about the caller's own site. 404 on another user's private site, exactly
    as for a site that doesn't exist. Uniqueness is re-checked against the *resulting* name and
    locality name, so moving a site to a place where that name is already taken is a 422. `latitude` and `longitude` are
    one value: a body naming one without the other is a 422, so moving a site means
    sending both and clearing it means sending both as null. The dives logged here read
    back the new name, place and position at once.

    Naming `location` **replaces** the stored place whole, so a body carrying only its name
    clears the previous centre and extent: send the whole place. An explicit `null` clears
    it, which is how a site entered with the wrong place is corrected back to "not recorded".

    A list - `other_names`, `external_ids`, `entry_types`, `tags` - replaces the stored one
    whole, and an explicit `null` clears a scalar. The depth range is checked against the
    stored end a body leaves out, so a `depth_from` below the stored `depth_to` is a 422. A
    rename onto one of the site's other names is a rename: that other name is dropped.

    While the site's proposal for the Commons waits, a body naming anything but `notes` and `tags`
    reaches the proposal, which keeps its place in the queue, its attestation and its message. Such
    a body clearing the pin is a 422 and one adding a registry entry a Commons record carries is a
    409, as proposing the site would answer, and either writes nothing.
    """
    # `location` is a value object with no identity, so there is nothing to merge a partial one
    # into; replacing it whole is what keeps a name-only body from carrying the previous
    # locality's centre and box forward.
    db_dive_site = await _get_readable_dive_site(db, uuid, current_user)
    owner_id = db_dive_site.user_id
    if owner_id is None:
        await _patch_saved_commons_site(db, db_dive_site, values, user_id=current_user["id"])
        return {"message": "Dive site updated"}
    sent = values.model_fields_set

    # Two gates, not one, because the two questions have different answers. Uniqueness is
    # a rule about name-at-locality and nothing else; staleness is about every field
    # `DiveSiteInfo` embeds, which is the whole place object and the position. Only
    # `latitude` is tested here: `WholeCoordinatePair` has already refused any body that
    # names one coordinate without the other, so longitude never travels alone.
    replaces_location = "location" in sent
    touches_name_or_location = values.name is not None or replaces_location
    touches_dive_summary = touches_name_or_location or "latitude" in sent
    effective_name = values.name if values.name is not None else db_dive_site.name
    effective_location_name = (
        (None if values.location is None else values.location.name) if replaces_location else db_dive_site.location_name
    )

    if touches_name_or_location and await dive_site_name_exists(
        db=db,
        user_id=owner_id,
        name=effective_name,
        location_name=effective_location_name,
        exclude_id=db_dive_site.id,
    ):
        raise DuplicateValueException(_NAME_TAKEN)

    # The schema checks a pair it was sent whole; against the stored end of one it was sent
    # half of, the route checks.
    if "depth_from" in sent or "depth_to" in sent:
        depth_from = values.depth_from if "depth_from" in sent else db_dive_site.depth_from
        depth_to = values.depth_to if "depth_to" in sent else db_dive_site.depth_to
        if depth_from is not None and depth_to is not None and depth_from > depth_to:
            raise UnprocessableEntityException(DEPTH_RANGE_MESSAGE)

    update_data = values.model_dump(exclude_unset=True, exclude={"location", "notes", "tags"})
    if replaces_location:
        update_data |= location_columns(values.location, DIVE_SITE_LOCATION_PREFIX)
    # Against the name the site ends up with, so a rename onto another name drops that one
    # from the stored list, and another name the stored name already says is dropped.
    if values.name is not None or values.other_names is not None:
        current = values.other_names if values.other_names is not None else db_dive_site.other_names
        conformed = canonical_other_names(effective_name, current)
        if values.other_names is not None or conformed != db_dive_site.other_names:
            update_data["other_names"] = conformed

    proposal = (
        await proposal_following_an_edit(db, site_id=db_dive_site.id, user_id=owner_id, changes=update_data)
        if update_data
        else None
    )

    tag_ids = None if values.tags is None else await _resolved_tag_ids(db, user_id=owner_id, names=values.tags)

    if update_data:
        try:
            await write_or_duplicate(
                db,
                # The owner too, which the UPDATE carries: a review may have given this row to the
                # Commons since it was read, and then the body names a shared member of a record.
                crud_dive_sites.update(db=db, object=update_data, uuid=uuid, user_id=owner_id, commit=False),
                DIVE_SITE_NAME_KEY,
                _NAME_TAKEN,
            )
        except NoResultFound as exc:
            raise ForbiddenException(_SHARED_MEMBER_REFUSED) from exc
        if proposal is not None:
            await follow_the_edit(db, proposal)
    if tag_ids is not None:
        await write_or_422(
            db, replace_tags_for_dive_site(db, db_dive_site.id, tag_ids, user_id=owner_id, commit=False), TAG_NOT_FOUND
        )
    if values.notes is not None and not await replace_saved_site_notes(
        db, user_id=owner_id, dive_site_id=db_dive_site.id, notes=values.notes
    ):
        raise NotFoundException("Dive site not found")

    if update_data or tag_ids is not None or values.notes is not None:
        await db.commit()
        await invalidate_dive_site_caches(owner_id)
        # Dive reads embed this site's name, locality and position, so a rename, a new
        # place or a dragged marker makes every cached dive logged here stale - the bug
        # that used to be documented as a known limitation, fixable now that the
        # single-dive cache key is user-scoped. The locality counts whole: a body that
        # only moves the place's centre still reshapes `DiveSiteInfo`. Those three and
        # nothing else - `DiveSiteInfo` carries no notes and none of the site's other
        # members, and dropping every cached dive a diver has because they retyped a
        # description would be a real cost for no staleness avoided.
        if touches_dive_summary:
            await invalidate_dive_caches(owner_id)

    return {"message": "Dive site updated"}


@router.delete("/dive-site/{uuid}")
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
    """Delete the caller's own dive site, or remove a saved Commons record from their list,
    optionally moving the dives logged at it to another site.

    A Commons record stays for everyone: what goes is the caller's notes and tags on it and
    their dives' links to it. 404 on a Commons record the caller has not saved, on another
    user's private site and on the uuid of a site the caller replaced with a Commons record,
    exactly as for a site that doesn't exist, and a second `DELETE` on the same uuid is a 404 too.

    The caller's dives logged here read back one site shorter, and nothing of the link survives
    the delete; pass `move_dives_to` if the association matters.

    Pass `move_dives_to` and every one of the caller's live dives logged here has this site
    swapped for that one first, in the same transaction as the delete: either the whole log
    moved and this site is gone, or nothing happened. The replacement takes this site's slot
    in each dive's ordered site list - so it inherits being the primary site if this one was
    - and a dive already logged at both ends up holding it once. The replacement may be any
    site on the caller's list or any Commons record, which the move saves for them. One that is
    neither, or that is this site, is a 422, the same answer `PATCH /dive` gives for a
    `dive_site_uuids` entry it can't resolve.

    The response is the bare `{"message": ...}` every other delete answers; the count of
    what moved is not reported.
    """
    # Not idempotent: one `DELETE FROM dive_site` in one transaction cannot half-fail, so a retry
    # has nothing to insure against - `DECISIONS.md`, *`DELETE` is not idempotent, and a second
    # call 404s*. `move_dives_to` is the per-dive `PATCH /dive` call made once, which is why a
    # bad replacement gets that route's 422 (*Move dives on delete: A bad replacement is a 422,
    # not a 404*); the unreported count is *Move dives on delete: The response carries no
    # moved-dives count*.
    #
    # Not through an alias: the phone's queued delete of a site since replaced would take the
    # record off the diver's list and every dive off it - `DECISIONS.md`, *A replaced site leaves
    # an alias*.
    db_dive_site = await _get_row_under_uuid(db, uuid, current_user)
    in_commons = db_dive_site.user_id is None
    owner_id = current_user["id"]
    if in_commons and not await is_saved(db, user_id=owner_id, dive_site_id=db_dive_site.id):
        raise NotFoundException("Dive site not found")

    if move_dives_to is not None:
        site_id_by_uuid = await resolve_dive_site_ids_for_user(db=db, dive_site_uuids=[move_dives_to], user_id=owner_id)
        if site_id_by_uuid is None:
            raise UnprocessableEntityException("Dive site not found.")
        # By the row, since a site the diver replaced with this record resolves to it.
        if site_id_by_uuid[move_dives_to] == db_dive_site.id:
            raise UnprocessableEntityException("A dive site cannot be moved onto itself.")
        await save_named_commons_sites(db, user_id=owner_id, dive_site_ids=[site_id_by_uuid[move_dives_to]])
        await replace_dive_site_on_dives(
            db=db,
            user_id=owner_id,
            from_dive_site_id=db_dive_site.id,
            to_dive_site_id=site_id_by_uuid[move_dives_to],
        )

    if in_commons:
        await remove_saved_commons_site(db, user_id=owner_id, dive_site_id=db_dive_site.id)
        await db.commit()
    else:
        # Commits the reassignment above along with the delete - `crud_dive_sites.delete` is the
        # only writer here that commits, and both wrote through this one session.
        await crud_dive_sites.delete(db=db, uuid=uuid)
    # This site's own read and the list pages, and - whichever branch ran - the replacement's
    # read, whose summary just gained this site's dives.
    await invalidate_dive_site_caches(owner_id)
    # The cascade shortens the site list of every dive logged here, so drop those reads too.
    await invalidate_dive_caches(owner_id)
    # And the site counts of every trip those dives are on, whichever branch ran: a move can
    # fold two of a trip's sites into one.
    await invalidate_trip_caches(owner_id)

    return {"message": "Dive site deleted"}
