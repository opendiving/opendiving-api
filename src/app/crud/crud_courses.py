import uuid as uuid_pkg
from datetime import date
from typing import Any

from fastcrud import FastCRUD
from sqlalchemy import ColumnElement, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.utils.search import search_clause
from ..models.course import Course
from ..schemas.course import CourseCreateInternal, CourseReadInternal, CourseUpdate, CourseUpdateInternal

CRUDCourse = FastCRUD[
    Course, CourseCreateInternal, CourseUpdate, CourseUpdateInternal, CourseUpdate, CourseReadInternal
]
crud_courses = CRUDCourse(Course)

# What a `search=` term is matched against. One column today; kept a tuple so the search
# clause reads the same as every other resource's and gains a second column for free.
COURSE_SEARCH_COLUMNS = ("name",)

# The course list's order: most recent course first, courses with no start date at all
# last. Matches `ix_course_user_id_start_date`, whose `start_date DESC NULLS LAST` can only
# serve a query asking for the same null placement - the identical pairing
# `_LIST_ORDER` in `crud_certifications` documents, and worth repeating here only because
# courses reach it from *both* their list branches rather than one.
#
# `NULLS LAST` is the behaviour as well as the index: a `planned` course has no dates yet,
# and so does a completed one somebody back-filled without them, so "no date" must not be
# read as "soonest". Postgres's default for `DESC` is `NULLS FIRST`, so it has to be
# spelled out.
#
# `uuid` breaks ties: it is uuid7, so it orders by creation time, which keeps pagination
# stable across pages when several courses share a start date (or have none).
_LIST_ORDER = (Course.start_date.desc().nulls_last(), Course.uuid.desc())


def _overlap_conditions(date_from: date | None, date_to: date | None) -> tuple[ColumnElement[bool], ...]:
    """The clauses matching courses whose dates overlap `[date_from, date_to]`, either bound
    independently absent.

    Overlap rather than containment: "courses I did in 2025" has to answer with the one that
    began in December 2024 and finished in February 2025, which is the whole point of asking
    by year. An open end (`start_date` set, `end_date` null) is an ongoing course and overlaps
    anything it started on or before.

    A course with *both* dates null has no interval to overlap, so it drops out of a
    date-filtered list rather than sitting at the top of it - which is where `NULLS LAST`
    would otherwise leave a `planned` course and a back-filled dateless one when a diver has
    asked a question about dates.

    An inverted window matches nothing, and that takes the explicit clause below rather than
    falling out of the other three: `end_date >= date_from AND start_date <= date_to` is still
    satisfiable with the bounds the wrong way round, by any course *spanning* the gap between
    them. That answer - a diver's longest courses - is the answer to a question nobody asked.
    The two bounds are one window in the UI that sends them, a From and a To on one row, so
    inverting them is a swap to show an empty list for, not a "covers this whole period" query
    to serve. An empty page rather than a 422 because nothing here is invalid: no such window
    has courses in it. `_validate_merged_date_range` is the write-side counterpart, where an
    end before a start really is a bad row.
    """
    if date_from is None and date_to is None:
        return ()

    if date_from is not None and date_to is not None and date_from > date_to:
        return (false(),)

    conditions: tuple[ColumnElement[bool], ...] = (or_(Course.start_date.is_not(None), Course.end_date.is_not(None)),)
    if date_from is not None:
        conditions += (or_(Course.end_date.is_(None), Course.end_date >= date_from),)
    if date_to is not None:
        conditions += (or_(Course.start_date.is_(None), Course.start_date <= date_to),)
    return conditions


async def get_courses_page(
    db: AsyncSession,
    *,
    user_id: int,
    offset: int,
    limit: int,
    search: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    agency: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """One page of a diver's courses, most recent first, in the same
    `{"data": [...], "total_count": n}` shape `crud.get_multi` returns.

    Hand-written for `_LIST_ORDER`, exactly as `get_certifications_page` is - and here the
    *searched* branch needs it too, which is what makes this one query rather than two.
    `core/utils/search.py::search_multi` would serve the search branch otherwise, but it
    builds a bare `.desc()`/`.asc()` from a single `sort_column`, so neither the null
    placement nor the `uuid` tie-break is reachable through it. Trips get away with
    `search_multi` only because `trip.start_date` is `NOT NULL`.

    Rows come back as plain dicts of every table column, matching `get_multi` called
    without a `schema_to_select`, so a caller can hand them to the same public-shape
    conversion either way.
    """
    conditions: tuple[ColumnElement[bool], ...] = (Course.user_id == user_id,)
    term = (search or "").strip()
    if term:
        conditions += (search_clause(Course, COURSE_SEARCH_COLUMNS, term),)
    conditions += _overlap_conditions(date_from, date_to)
    # Plain equality on columns the route has already narrowed to `CertificationAgency` and
    # `CourseStatus`, so an unknown value is a 422 there rather than an empty page here.
    if agency is not None:
        conditions += (Course.agency == agency,)
    if status is not None:
        conditions += (Course.status == status,)

    total_count = await db.scalar(select(func.count()).select_from(Course).where(*conditions))
    rows = (
        await db.execute(
            select(*Course.__table__.columns).where(*conditions).order_by(*_LIST_ORDER).offset(offset).limit(limit)
        )
    ).mappings()

    return {"data": [dict(row) for row in rows], "total_count": total_count or 0}


async def resolve_course_id_for_user(db: AsyncSession, course_uuid: uuid_pkg.UUID, user_id: int) -> int | None:
    """Resolve a course's public `uuid` to its internal `id`, scoped to a course belonging
    to the given user.

    The twin of `resolve_trip_id_for_user`: it translates a client-supplied course
    reference into the internal id needed for FK storage, and the `user_id` scope is what
    stops a diver linking somebody else's course to their own dive or certification.
    """
    stmt = select(Course.id).where(
        Course.uuid == course_uuid,
        Course.user_id == user_id,
    )
    result = await db.execute(stmt.limit(1))
    row = result.first()
    return row[0] if row is not None else None


async def get_course_uuids_by_ids(db: AsyncSession, course_ids: list[int], user_id: int) -> dict[int, uuid_pkg.UUID]:
    """Batched lookup of course `id` -> `uuid`, for enriching a paginated dive or
    certification listing without a query per row.

    A miss is not an expected outcome, exactly as for `get_trip_uuids_by_ids`: courses are
    hard-deleted and both referring columns are `ON DELETE SET NULL`, so deleting a course
    clears the column on every row that pointed at it rather than leaving an id behind for
    this lookup to decline. The callers' `None` comes from the row itself.

    The `user_id` scope is defence in depth rather than a fix - today every caller passes
    ids taken from the caller's own rows - so that a future caller sourcing ids some other
    way cannot leak a uuid.
    """
    if not course_ids:
        return {}

    result = await db.execute(
        select(Course.id, Course.uuid).where(
            Course.id.in_(set(course_ids)),
            Course.user_id == user_id,
        )
    )
    return {row.id: row.uuid for row in result}
