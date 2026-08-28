import uuid as uuid_pkg

from fastcrud import FastCRUD
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.course import Course
from ..schemas.course import CourseCreateInternal, CourseReadInternal, CourseUpdate, CourseUpdateInternal

CRUDCourse = FastCRUD[Course, CourseCreateInternal, CourseUpdate, CourseUpdateInternal, CourseUpdate, CourseReadInternal]
crud_courses = CRUDCourse(Course)


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
