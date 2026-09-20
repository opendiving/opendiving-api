import uuid as uuid_pkg
from datetime import date
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query, Request
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import fetch_owned_or_raise, get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import (
    NotFoundException,
    UnprocessableEntityException,
)
from ...core.schemas import validate_date_range
from ...core.utils.cache import cache
from ...core.utils.owned_resource_cache import OwnedResourceCache
from ...core.utils.pagination import clamp_pagination
from ...crud.crud_courses import COURSE_SEARCH_COLUMNS, crud_courses, get_courses_page
from ...schemas.certification import CertificationAgency, validate_agency_pairing
from ...schemas.course import (
    CourseCreate,
    CourseCreateInternal,
    CourseRead,
    CourseReadInternal,
    CourseStatus,
    CourseUpdate,
)
from ...services.cache_invalidation import (
    invalidate_certification_caches,
    invalidate_course_caches,
    invalidate_dive_caches,
)

router = APIRouter(tags=["courses"])


def _to_public_course(db_course: CourseReadInternal | dict[str, Any], *, user_uuid: uuid_pkg.UUID) -> CourseRead:
    """Convert an internal course representation (integer PK/FK) into its public shape
    (owning user referenced by `uuid`)."""
    data = db_course if isinstance(db_course, dict) else db_course.model_dump()
    return CourseRead(**{k: v for k, v in data.items() if k not in ("id", "user_id")}, user_uuid=user_uuid)


# Kept for its `list_cache_key_prefix` and `invalidate_list` only - `read_list`/`read_item`
# are never called, exactly as in `trips.py`. Its `search_columns` being non-empty is what
# keeps the `:search:{search}` segment in the key, and that segment is load-bearing: two
# different searches on the same page must not serve each other's results.
#
# The invariant the key encodes: every dimension a list read varies on appears in it. Page,
# size and the search term come from here; this list's four filters are appended to the
# prefix at `_LIST_CACHE_KEY_PREFIX` below.
_course_cache: OwnedResourceCache[CourseReadInternal, CourseRead] = OwnedResourceCache(
    resource_name="courses",
    resource_label="Course",
    item_cache_prefix="user_{user_id}_course",
    crud=crud_courses,
    schema_to_select=CourseReadInternal,
    to_public=lambda db_course, user_uuid: _to_public_course(db_course, user_uuid=user_uuid),
    sort_columns="start_date",
    sort_orders="desc",
    search_columns=COURSE_SEARCH_COLUMNS,
)


def _validate_merged_agency_pairing(agency: str | None, agency_other: str | None) -> None:
    """Enforce the `agency`/`agency_other` pairing on a PATCH's merged result.

    `CourseBase` already does this for whole-object writes, but a PATCH may carry either
    field alone, so the check can only be made once the incoming values have been merged
    over the stored ones - the same shape `patch_certification` uses.

    A course's `agency` is nullable, so the merged value may be `None` and clearing it
    while an `agency_other` still names one is the 422 this produces.
    """
    try:
        validate_agency_pairing(agency, agency_other)
    except ValueError as e:
        raise UnprocessableEntityException(str(e)) from e


def _validate_merged_date_range(start_date: date | None, end_date: date | None) -> None:
    """Enforce `end_date >= start_date` on a PATCH's merged result.

    `CourseUpdate` cannot see a pair whose other half is already stored, and the table's
    `ck_course_date_range` answers with an `IntegrityError` rather than a sentence naming
    the fields. The `ck_course_date_range` constraint would
    refuse the write anyway, so this is about answering with a sentence naming the fields
    rather than with an `IntegrityError`.
    """
    try:
        validate_date_range(start_date, end_date)
    except ValueError as e:
        raise UnprocessableEntityException(str(e)) from e


async def _get_owned_course(db: AsyncSession, uuid: uuid_pkg.UUID, current_user: dict) -> CourseReadInternal:
    """Fetch a course by public uuid and assert the caller owns it.

    Thin wrapper over `fetch_owned_or_raise` - see there for why someone else's row reads
    as a 404 and, in particular, why this must run before any `@cache`-wrapped read
    helper.
    """
    return await fetch_owned_or_raise(
        db=db,
        crud=crud_courses,
        uuid=uuid,
        current_user=current_user,
        schema=CourseReadInternal,
        not_found_message="Course not found",
    )


@router.post("/course", response_model=CourseRead, status_code=201)
async def write_course(
    request: Request,
    course: CourseCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> CourseRead:
    """Create a training course for the authenticated user.

    Unlike trips, course names are **not** unique per user - a course failed once and
    retaken later is legitimately the same name twice - so there is no duplicate-name
    refusal here.

    Dives and certifications are linked to a course from their own endpoints
    (`course_uuid` on `POST`/`PATCH /dive` and `/certification`), not from here.
    """
    course_internal = CourseCreateInternal(**course.model_dump(), user_id=current_user["id"])
    created = await crud_courses.create(
        db=db, object=course_internal, schema_to_select=CourseReadInternal, return_as_model=True
    )
    await invalidate_course_caches(current_user["id"])

    return _to_public_course(cast(CourseReadInternal, created), user_uuid=current_user["uuid"])


# Every dimension this list varies on has to appear in the key, or one filter's page is
# served to another filter's request - the defect the `:search:` segment already exists to
# prevent. The factory's prefix is the base, so that half stays byte-identical to what every
# other list resource writes; the four filters are appended here rather than in
# `OwnedResourceCache`, which builds one prefix for resources that do not have them. Like the
# dive list's key, every placeholder is looked up as a **keyword** argument, so a filter
# passed positionally raises at key construction rather than quietly collapsing two different
# result sets onto one entry.
_LIST_CACHE_KEY_PREFIX = (
    _course_cache.list_cache_key_prefix + ":date_from:{date_from}:date_to:{date_to}:agency:{agency}:status:{status}"
)


@cache(
    key_prefix=_LIST_CACHE_KEY_PREFIX,
    resource_id_name="user_id",
    expiration=60,
)
async def _cached_read_courses(
    request: Request,
    user_id: int,
    user_uuid: uuid_pkg.UUID,
    db: AsyncSession,
    page: int,
    items_per_page: int,
    search: str | None,
    date_from: date | None,
    date_to: date | None,
    agency: str | None,
    status: str | None,
) -> dict:
    """Fetches (and caches) a user's paginated course list.

    Only ever called after `read_courses` below has checked the caller's authorization - a
    `@cache` hit skips this body entirely, authorization logic included.

    The kwarg names are load-bearing: every one of them fills a placeholder in
    `_LIST_CACHE_KEY_PREFIX`, which is what keeps the keys inside the `user_{id}_course*`
    pattern `invalidate_course_caches` sweeps.

    `get_courses_page` serves both the searched and the unsearched branch from one
    hand-written `select()`, rather than the `search_multi`/`get_multi` pair every other
    list resource uses - see its docstring for why neither can produce this ordering.
    """
    courses_data = await get_courses_page(
        db=db,
        user_id=user_id,
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
        search=search,
        date_from=date_from,
        date_to=date_to,
        agency=agency,
        status=status,
    )
    courses_data["data"] = [_to_public_course(row, user_uuid=user_uuid).model_dump() for row in courses_data["data"]]

    response: dict[str, Any] = paginated_response(crud_data=courses_data, page=page, items_per_page=items_per_page)
    return response


@router.get("/courses", response_model=PaginatedListResponse[CourseRead])
async def read_courses(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: int = 1,
    items_per_page: int = 10,
    search: Annotated[
        str | None,
        Query(max_length=255, description="Case-insensitive substring match on the course's name"),
    ] = None,
    date_from: Annotated[
        date | None,
        Query(description="Start of a window a course's own dates must overlap; alone, courses not yet over by it"),
    ] = None,
    date_to: Annotated[
        date | None,
        Query(description="End of that window; alone, courses that had started by it"),
    ] = None,
    agency: Annotated[
        CertificationAgency | None,
        Query(description="Only courses that ran under this training agency"),
    ] = None,
    status: Annotated[
        CourseStatus | None,
        Query(description="Only courses in this state"),
    ] = None,
) -> dict:
    """List the caller's training courses, most recent start date first.

    Courses with no dates yet sort **last**, not first, so a `planned` course and a
    back-filled one without dates stay out of the way of the log's chronology. `search`
    matches a case-insensitive substring of the name. Out-of-range pagination is clamped
    rather than rejected.

    All four filters combine with each other and with `search`. `date_from`/`date_to`
    describe a window a course's own `[start_date, end_date]` has to **overlap**, so
    `2025-01-01`..`2025-12-31` answers "courses I did in 2025" with the one that began that
    December and finished in February - and a course with neither date drops out of the list
    entirely once either bound is set, having no interval to overlap. Either bound stands on
    its own: `date_from` alone is "still running on or after that day", `date_to` alone is
    "had started by it". A window with its bounds the wrong way round is a swap rather than a
    refusal - it matches nothing, so the client shows the same empty list it shows for a
    window with no courses in it.

    `agency` and `status` are exact matches on the stored value. Unlike the uuid filters on
    `GET /dives`, a value outside the vocabulary is a 422 rather than an empty page: those
    name a resource whose existence must stay unprobeable, while these name a member of a
    closed set the client already has.
    """
    page, items_per_page = clamp_pagination(page, items_per_page)

    return await _cached_read_courses(
        request,
        user_id=current_user["id"],
        user_uuid=current_user["uuid"],
        db=db,
        page=page,
        items_per_page=items_per_page,
        # Normalized here rather than in the cache layer so that " TDI " and "tdi" share
        # one cache entry instead of two identical ones under different keys. The other four
        # need no equivalent: each has exactly one spelling by the time it is parsed, an
        # unparseable one (`?agency=` included) having been a 422 before this line.
        search=(search or "").strip().lower() or None,
        date_from=date_from,
        date_to=date_to,
        # The column holds a plain string (see `StoredVocabulary`); the enum's job is done
        # once it has made an unknown value a 422 above.
        agency=agency.value if agency is not None else None,
        status=status.value if status is not None else None,
    )


@cache(key_prefix="user_{user_id}_course", resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)
async def _cached_read_course(
    request: Request, user_id: int, uuid: uuid_pkg.UUID, owner_uuid: uuid_pkg.UUID, db: AsyncSession
) -> CourseRead:
    """Fetches (and caches) a single course by uuid. Authorization is checked by the route
    before this is ever reached - see `_cached_read_courses`.

    Keyed under the same `user_{id}_course` prefix as the list pages so one invalidation
    pattern covers both.
    """
    db_course = await crud_courses.get(db=db, uuid=uuid, schema_to_select=CourseReadInternal, return_as_model=True)
    if db_course is None:
        raise NotFoundException("Course not found")

    return _to_public_course(cast(CourseReadInternal, db_course), user_uuid=owner_uuid)


@router.get("/course/{uuid}", response_model=CourseRead)
async def read_course(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> CourseRead:
    """Return a single training course by its public uuid.

    404 when no such course exists - and the same 404 when it belongs to another user, so
    someone else's uuid stays unprobeable. The dives and certifications linked to it come
    from `GET /dives?course_uuid=` and `GET /certifications?course_uuid=`.
    """
    # Authorize before the cached read: `@cache` replays a hit without re-checking.
    await _get_owned_course(db, uuid, current_user)

    return await _cached_read_course(
        request, user_id=current_user["id"], uuid=uuid, owner_uuid=current_user["uuid"], db=db
    )


@router.patch("/course/{uuid}")
async def patch_course(
    request: Request,
    uuid: uuid_pkg.UUID,
    values: CourseUpdate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Partially update a training course; omitted fields are left untouched.

    404 unless the caller owns it, exactly as for a course that doesn't exist. `agency` and
    `agency_other` are validated as a pair against the resulting values, so clearing one
    while the other still requires it is a 422 rather than a half-updated row - and
    `start_date`/`end_date` likewise, so moving either one past the stored other is a 422
    rather than a course that ends before it began.
    """
    db_course = await _get_owned_course(db, uuid, current_user)

    update_data = values.model_dump(exclude_unset=True)
    if "agency" in update_data or "agency_other" in update_data:
        _validate_merged_agency_pairing(
            update_data.get("agency", db_course.agency),
            update_data.get("agency_other", db_course.agency_other),
        )
    if "start_date" in update_data or "end_date" in update_data:
        _validate_merged_date_range(
            update_data.get("start_date", db_course.start_date),
            update_data.get("end_date", db_course.end_date),
        )

    if update_data:
        await crud_courses.update(db=db, object=update_data, uuid=uuid)
        await invalidate_course_caches(db_course.user_id)

    return {"message": "Course updated"}


@router.delete("/course/{uuid}")
async def erase_course(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Delete a training course. The dives and certifications on it survive, unlinked.

    404 unless the caller owns it, exactly as for a course that doesn't exist - and a
    second `DELETE` on the same uuid is a 404 too, because the row really is gone.

    There is no reassign-before-delete parameter, unlike `DELETE /trip/{uuid}`: a trip
    groups a whole holiday's dives and moving them somewhere else is a real operation,
    while a deleted course simply unlinks. `dive.course_id` and `certification.course_id`
    are both `ON DELETE SET NULL`, so the database does that itself, and there is no way
    back after.
    """
    db_course = await _get_owned_course(db, uuid, current_user)
    owner_id = db_course.user_id

    await crud_courses.delete(db=db, uuid=uuid)

    await invalidate_course_caches(owner_id)
    # Three families, not one. Every dive and every certification that pointed at this
    # course just had its `course_id` nulled by the FK, so their cached reads would go on
    # naming a course fresh ones no longer do - the same reason `erase_trip` invalidates
    # the dive caches unconditionally.
    await invalidate_dive_caches(owner_id)
    await invalidate_certification_caches(owner_id)

    return {"message": "Course deleted"}
