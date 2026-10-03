import uuid as uuid_pkg
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query, Request
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy.exc import IntegrityError
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
from ...crud.crud_contacts import resolve_contact_ids_for_user
from ...crud.crud_dives import reassign_dives_to_trip
from ...crud.crud_people import get_people_for_trips, replace_people_for_trip
from ...crud.crud_trip_parts import (
    get_parts_for_trip,
    get_parts_for_trips,
    replace_parts_for_trip,
)
from ...crud.crud_trips import (
    NO_DIVES,
    TripFigures,
    crud_trips,
    get_figures_for_trips,
    get_trips_page,
    resolve_trip_id_for_user,
    trip_name_exists,
)
from ...schemas.person import PersonReferenceRead
from ...schemas.trip import (
    TripCreate,
    TripCreateInternal,
    TripPartInput,
    TripPartRead,
    TripRead,
    TripReadInternal,
    TripUpdateRequest,
)
from ...services.cache_invalidation import invalidate_dive_caches, invalidate_trip_caches
from ...services.person_links import resolve_people_references

router = APIRouter(tags=["trips"])

# The only integrity failures the part and people writes below can hit are the trip row, a
# contact a part stays at or a person on the trip vanishing between the resolve and the
# insert (a concurrent hard delete): lengths and ranges are already bounded by
# `TripPartInput`, and the people write drops a repeated person before
# `ux_trip_person_trip_id_person_id` could refuse it. 422 rather than a raw 500,
# matching how `patch_dive` treats its child-row writes.
#
# Both routes raise before invalidating anything, so this path knowingly leaves the trip's
# caches as they were - including any column update `patch_trip` already committed. Same
# as `patch_dive`, and the trade is deliberate: the trigger needs a concurrent delete, and
# invalidating on the way out of a failed write would mean doing it in two places for a
# race that narrow.
_PART_ERROR_DETAIL = "Trip parts could not be saved."
_PEOPLE_ERROR_DETAIL = "The trip's people could not be saved."


async def _resolve_accommodations(
    db: AsyncSession, parts: list[TripPartInput], user_id: int
) -> dict[uuid_pkg.UUID, int]:
    """The contacts the parts stay at, resolved against the trip's owner before anything is
    written. One that is not the caller's own - or does not exist - is a 422, the answer a
    foreign course gets on a certification, and it comes before the trip row so a refused
    part cannot leave a trip behind without its parts."""
    wanted = [part.accommodation_uuid for part in parts if part.accommodation_uuid is not None]
    contact_ids = await resolve_contact_ids_for_user(db=db, contact_uuids=wanted, user_id=user_id)
    if contact_ids is None:
        raise UnprocessableEntityException("Contact not found.")
    return contact_ids


async def _get_owned_trip(db: AsyncSession, uuid: uuid_pkg.UUID, current_user: dict) -> TripReadInternal:
    """Fetch a trip by public uuid and assert the caller owns it.

    Thin wrapper over `fetch_owned_or_raise` - see there for why someone else's row reads
    as a 404 and, in particular, why this must run before any `@cache`-wrapped read
    helper.
    """
    return await fetch_owned_or_raise(
        db=db,
        crud=crud_trips,
        uuid=uuid,
        current_user=current_user,
        schema=TripReadInternal,
        not_found_message="Trip not found",
    )


def _to_public_trip(
    db_trip: TripReadInternal | dict[str, Any],
    *,
    user_uuid: uuid_pkg.UUID,
    figures: TripFigures,
    parts: list[TripPartRead] | None = None,
    people: list[PersonReferenceRead] | None = None,
) -> TripRead:
    """Convert an internal trip representation (integer FKs) into its public shape
    (owning user referenced by `uuid`, parts and people embedded as read from the child
    tables, and the figures over its dives).
    """
    data = db_trip if isinstance(db_trip, dict) else db_trip.model_dump()
    return TripRead(
        **{k: v for k, v in data.items() if k not in ("id", "user_id")},
        **figures._asdict(),
        user_uuid=user_uuid,
        parts=parts or [],
        people=people or [],
    )


# Kept for its `list_cache_key_prefix` only - `read_list`, and so `to_public`, is never
# called, and like `dive_sites.py`'s it declares no `sort_columns`, the list's ordering
# being an aggregate over `trip_part` rather than a column of `trip`. A trip read
# also embeds its parts and counts its dives, which are further queries zipped back into the
# page (see the factory's docstring, which lists `dives.py` and the other opt-outs). The
# single read is hand-written too, under `user_{id}_trip` so that
# `invalidate_trip_caches` drops it by pattern along with the list, as courses do.
#
# `search_columns` still has to be non-empty: it is what keeps the `:search:{search}`
# segment in the key. Only `name` remains a column - a trip's places are rows in
# `trip_part`, and `crud_trips.search_conditions` is what searches them.
_trip_cache: OwnedResourceCache[TripReadInternal, TripRead] = OwnedResourceCache(
    resource_name="trips",
    crud=crud_trips,
    to_public=lambda db_trip, user_uuid: _to_public_trip(db_trip, user_uuid=user_uuid, figures=NO_DIVES),
    search_columns=("name",),
)


@router.post("/trip", response_model=TripRead, status_code=201)
async def write_trip(
    request: Request,
    trip: TripCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> TripRead:
    """Create a trip for the authenticated user, together with the parts it ran.

    Trip names are unique per user, so reusing one that already exists is a 422. A trip
    carries no dates of its own: each part has its own optional range and its own optional
    place, and the trip's span is the earliest start and latest end across them. `parts`
    keep the order given; index 0 is the one shown wherever only a single part fits. A part's
    `accommodation_uuid` names the contact the diver stayed at; one that isn't the caller's
    own - or doesn't exist - is a 422. `people` names who came on the trip, each with a role
    or none, and is refused the same way.

    The trip row commits before its parts do, so a failure inserting them leaves the trip
    behind without them - the same accepted semantics as `POST /dive` and its mixtures.
    """
    if await trip_name_exists(db=db, user_id=current_user["id"], name=trip.name):
        raise DuplicateValueException("A trip with this name already exists")
    accommodation_ids = await _resolve_accommodations(db, trip.parts, current_user["id"])
    people = await resolve_people_references(db, trip.people, user_id=current_user["id"])

    # Only the trip's own columns reach `TripCreateInternal`, which is `extra="forbid"`:
    # parts are rows in another table.
    trip_internal = TripCreateInternal(name=trip.name, notes=trip.notes, user_id=current_user["id"])
    created_trip = await crud_trips.create(
        db=db, object=trip_internal, schema_to_select=TripReadInternal, return_as_model=True
    )

    try:
        await replace_parts_for_trip(
            db=db, trip_id=created_trip.id, parts=trip.parts, accommodation_ids=accommodation_ids
        )
    except IntegrityError as e:
        await db.rollback()
        raise UnprocessableEntityException(_PART_ERROR_DETAIL) from e
    if people:
        try:
            await replace_people_for_trip(db=db, trip_id=created_trip.id, references=people)
        except IntegrityError as e:
            await db.rollback()
            raise UnprocessableEntityException(_PEOPLE_ERROR_DETAIL) from e

    await invalidate_trip_caches(current_user["id"])

    trip_read = await crud_trips.get(db=db, id=created_trip.id, schema_to_select=TripReadInternal, return_as_model=True)
    if trip_read is None:
        raise NotFoundException("Created trip not found")

    stored_parts = await get_parts_for_trip(db=db, trip_id=created_trip.id)
    stored_people = (await get_people_for_trips(db, [created_trip.id]))[created_trip.id] if people else []
    # A dive can only name a trip that already exists, so a new one has none.
    return _to_public_trip(
        cast(TripReadInternal, trip_read),
        user_uuid=current_user["uuid"],
        figures=NO_DIVES,
        parts=stored_parts,
        people=stored_people,
    )


@cache(
    key_prefix=_trip_cache.list_cache_key_prefix,
    resource_id_name="user_id",
    expiration=60,
)
async def _cached_read_trips(
    request: Request,
    user_id: int,
    user_uuid: uuid_pkg.UUID,
    db: AsyncSession,
    page: int,
    items_per_page: int,
    search: str | None,
) -> dict:
    """Fetches (and caches) a user's paginated trip list, each trip with its parts, its people
    and the figures over its dives.

    Only ever called after `read_trips` below has checked the caller's authorization - a
    `@cache` hit skips this body entirely, authorization logic included.

    The kwarg names are load-bearing: `user_id`, `page`, `items_per_page` and `search`
    fill the placeholders in the key prefix this borrows from `_trip_cache`, which is
    what keeps the keys inside the list pattern `invalidate_trip_caches` sweeps.
    """
    trips_data = await get_trips_page(
        db=db,
        user_id=user_id,
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
        search=search,
    )

    # One batched query for the page rather than one per trip. `get_trips_page` returns
    # full-column dicts, matching `get_multi` without a `schema_to_select`, so the internal
    # `id` the child rows hang off is there to read.
    trip_ids = [trip["id"] for trip in trips_data["data"]]
    parts_by_trip = await get_parts_for_trips(db=db, trip_ids=trip_ids)
    people_by_trip = await get_people_for_trips(db, trip_ids)
    figures_by_trip = await get_figures_for_trips(db, trip_ids=trip_ids, user_id=user_id)

    trips_data["data"] = [
        _to_public_trip(
            trip,
            user_uuid=user_uuid,
            figures=figures_by_trip[trip["id"]],
            parts=parts_by_trip.get(trip["id"], []),
            people=people_by_trip.get(trip["id"]),
        ).model_dump()
        for trip in trips_data["data"]
    ]

    response: dict[str, Any] = paginated_response(crud_data=trips_data, page=page, items_per_page=items_per_page)
    return response


@router.get("/trips", response_model=PaginatedListResponse[TripRead])
async def read_trips(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: int = 1,
    items_per_page: int = 10,
    search: Annotated[
        str | None,
        Query(max_length=255, description="Case-insensitive substring match on the trip's name or its places"),
    ] = None,
) -> dict:
    """List the caller's trips, most recent first, each with its parts and people.

    Each trip also counts its live dives, the distinct dive sites they name and the distinct
    species recorded on them - `0` for a trip no dive is on - and carries the deepest of those
    dives' `max_depth`, `null` when none recorded one.

    A trip's position in the list is the earliest start date across its parts; a trip
    whose parts carry no dates at all sorts after every trip that has one.

    `search` matches a case-insensitive substring against the trip's name and the names
    of the places its parts went to, so a trip is findable by either. Out-of-range
    pagination is clamped rather than rejected, so `items_per_page` above the ceiling
    returns the ceiling instead of a 422.
    """
    page, items_per_page = clamp_pagination(page, items_per_page)

    return await _cached_read_trips(
        request,
        user_id=current_user["id"],
        user_uuid=current_user["uuid"],
        db=db,
        page=page,
        items_per_page=items_per_page,
        # Normalized here rather than in the cache layer so that " Dahab " and "dahab"
        # share one cache entry instead of two identical ones under different keys.
        search=(search or "").strip().lower() or None,
    )


@cache(key_prefix="user_{user_id}_trip", resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)
async def _cached_read_trip(
    request: Request, user_id: int, uuid: uuid_pkg.UUID, owner_uuid: uuid_pkg.UUID, db: AsyncSession
) -> dict[str, Any]:
    """Fetches (and caches) a single trip by uuid, with its parts, its people and the figures
    over its dives attached.

    Like `_cached_read_trips`, this must only be called once the route has established
    that the caller owns the trip: `@cache` can serve a hit without re-checking it.
    """
    db_trip = await crud_trips.get(db=db, uuid=uuid, schema_to_select=TripReadInternal, return_as_model=True)
    if db_trip is None:
        raise NotFoundException("Trip not found")
    db_trip = cast(TripReadInternal, db_trip)

    parts = await get_parts_for_trip(db=db, trip_id=db_trip.id)
    people = (await get_people_for_trips(db, [db_trip.id])).get(db_trip.id)
    figures = (await get_figures_for_trips(db, trip_ids=[db_trip.id], user_id=user_id))[db_trip.id]
    return _to_public_trip(db_trip, user_uuid=owner_uuid, figures=figures, parts=parts, people=people).model_dump()


@router.get("/trip/{uuid}", response_model=TripRead)
async def read_trip(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    """Return a single trip by its public uuid, with the parts it ran, the people on it and
    the figures over its dives that `GET /trips` carries.

    404 when no such trip exists - and the same 404 when it belongs to another user, so
    someone else's uuid stays unprobeable.
    """
    # Authorize before the cached read: `@cache` replays a hit without re-checking.
    await _get_owned_trip(db, uuid, current_user)

    return await _cached_read_trip(
        request, user_id=current_user["id"], uuid=uuid, owner_uuid=current_user["uuid"], db=db
    )


@router.patch("/trip/{uuid}")
async def patch_trip(
    request: Request,
    uuid: uuid_pkg.UUID,
    values: TripUpdateRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Partially update a trip; omitted fields are left untouched.

    404 unless the caller owns it, exactly as for a trip that doesn't exist. Renaming to
    a name the caller already has on another trip is a 422. `parts` is replaced wholesale
    when present rather than merged, so sending a shorter list removes the difference and
    an empty list clears them; omitting the key leaves them alone. Each part's own
    `end_date` must be on or after its `start_date`, which is a 422 naming the part, and a
    part's `accommodation_uuid` must name one of the caller's contacts. `people` is replaced
    the same way as `parts`, and every person in it must be the caller's.
    """
    db_trip = await _get_owned_trip(db, uuid, current_user)

    if values.name is not None and await trip_name_exists(
        db=db, user_id=db_trip.user_id, name=values.name, exclude_id=db_trip.id
    ):
        raise DuplicateValueException("A trip with this name already exists")

    update_data = values.model_dump(
        include={"name", "notes"},
        exclude_unset=True,
    )

    # `None` leaves the existing parts alone; `[]` is a diver clearing them.
    parts = values.parts
    accommodation_ids = {} if parts is None else await _resolve_accommodations(db, parts, db_trip.user_id)
    people = (
        None if values.people is None else await resolve_people_references(db, values.people, user_id=db_trip.user_id)
    )

    if update_data:
        await crud_trips.update(db=db, object=update_data, uuid=uuid)

    if parts is not None:
        try:
            await replace_parts_for_trip(db=db, trip_id=db_trip.id, parts=parts, accommodation_ids=accommodation_ids)
        except IntegrityError as e:
            await db.rollback()
            raise UnprocessableEntityException(_PART_ERROR_DETAIL) from e

    if people is not None:
        try:
            await replace_people_for_trip(db=db, trip_id=db_trip.id, references=people)
        except IntegrityError as e:
            await db.rollback()
            raise UnprocessableEntityException(_PEOPLE_ERROR_DETAIL) from e

    # A parts- or people-only edit has an empty `update_data` but still changes what every
    # read of this trip says.
    if update_data or parts is not None or people is not None:
        await invalidate_trip_caches(db_trip.user_id)

    return {"message": "Trip updated"}


@router.delete("/trip/{uuid}")
async def erase_trip(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    move_dives_to: Annotated[
        uuid_pkg.UUID | None,
        Query(description="Move this trip's dives onto the trip with this uuid before deleting it"),
    ] = None,
) -> dict[str, str]:
    """Delete a trip, optionally moving its dives onto another trip first.

    404 unless the caller owns it, exactly as for a trip that doesn't exist - and a second
    `DELETE` on the same uuid is now a 404 too, because the row really is gone. This route
    used to be idempotent to insure against a half-failed multi-statement delete; one
    `DELETE FROM trip` in one transaction cannot half-fail.

    `trip_part` and `trip_person` rows go with it (`ON DELETE CASCADE`) and the dives logged on it survive
    with `trip_id` nulled (`ON DELETE SET NULL`) - both rules were already declared on the
    FKs and finally fire. Re-point the dives with `move_dives_to` before deleting if the
    association matters; there is no way back after.

    Pass `move_dives_to` and every one of the caller's live dives on this trip is
    re-pointed at that one first, in the same transaction as the delete: either the diver's
    log ends up entirely on the replacement trip with this one gone, or nothing happened.
    A replacement that isn't the caller's own trip, or that is this trip, is a 422 - the
    same answer `PATCH /dive` gives for a `trip_uuid` it can't resolve, which is the
    per-dive call this parameter exists to replace.

    The response is the bare `{"message": ...}` every other delete on the API returns; the
    count of what moved is not reported. See DECISIONS.md.
    """
    db_trip = await _get_owned_trip(db, uuid, current_user)
    owner_id = db_trip.user_id

    if move_dives_to is not None:
        if move_dives_to == uuid:
            raise UnprocessableEntityException("A trip cannot be moved onto itself.")
        replacement_id = await resolve_trip_id_for_user(db=db, trip_uuid=move_dives_to, user_id=owner_id)
        if replacement_id is None:
            raise UnprocessableEntityException("Trip not found.")
        await reassign_dives_to_trip(db=db, user_id=owner_id, from_trip_id=db_trip.id, to_trip_id=replacement_id)

    # Commits the reassignment above along with the delete - `crud_trips.delete` is the
    # only writer here that commits, and both wrote through this one session.
    await crud_trips.delete(db=db, uuid=uuid)
    # Every trip read, not only this one's: a move changes the replacement's figures.
    await invalidate_trip_caches(owner_id)
    # Unconditional, like `erase_dive_site`. Either branch changes what this user's dives
    # report: a move rewrites each moved dive's `trip_uuid`, and a plain delete nulls
    # `dive.trip_id` outright, so every dive that was on this trip reads back
    # `trip_uuid: null`. Skipping the plain-delete case would leave the cached reads naming
    # a trip fresh ones no longer do, for the rest of the hour.
    await invalidate_dive_caches(owner_id)

    return {"message": "Trip deleted"}
