"""Unit tests for the course endpoints (`api/v1/courses.py`), the query behind them
(`crud/crud_courses.py`) and the request schemas (`schemas/course.py`).

House style per `test_trips.py`: mostly unit tests with the route's collaborators stubbed
and the assertions on what it hands them, plus a Postgres-guarded tail for the things only
the database settles.

Four behaviours here are not guard-driven - nothing anywhere else fails if they regress -
and they are why this module exists rather than the endpoints being taken on trust:

* **the merged-value PATCH checks.** `CourseUpdate` deliberately validates neither the
  `agency`/`agency_other` pairing nor the date ordering, because a PATCH may carry either
  half of either pair alone. The route re-checks both against the stored row, which is the
  behaviour [api #127] added for trips;
* **`NULLS LAST` with a `uuid` tie-break**, which is the whole reason the list read is a
  hand-written `select()` rather than `get_multi`. Asserted as compiled SQL here and
  against a real Postgres below - the placement is a property of the database, and
  `TestListOrderingAgainstPostgres` is the half that would catch a bare `desc()`;
* **delete unlinking both children while their rows survive** - the `ON DELETE SET NULL`
  the migration declares and nothing else exercises;
* **the three-family cache invalidation** a delete has to do, which no test above the
  route can see.

The last two classes run against a live Postgres and skip themselves when none is
reachable; on a developer's machine that needs `POSTGRES_SERVER=localhost`. See
CONTRIBUTING.md.
"""

import uuid as uuid_pkg
from datetime import UTC, date, datetime
from fnmatch import fnmatch
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1 import courses as courses_module
from src.app.core.exceptions.http_exceptions import (
    NotFoundException,
    UnprocessableEntityException,
)
from src.app.core.utils import cache as cache_module
from src.app.core.utils.cache import across_builds, namespaced
from src.app.crud.crud_courses import (
    _LIST_ORDER,
    crud_courses,
    get_course_uuids_by_ids,
    get_courses_page,
    resolve_course_id_for_user,
)
from src.app.models.certification import Certification
from src.app.models.course import Course
from src.app.models.dive import Dive
from src.app.models.user import User
from src.app.schemas.certification import CertificationAgency
from src.app.schemas.course import CourseCreate, CourseReadInternal, CourseStatus, CourseUpdate
from tests.conftest import db_available
from tests.helpers.generators import create_certification, create_course, create_dive

USER_ID = 1
USER_UUID = uuid7()

# The undecorated bodies. `@cache` would need Redis and would serve a hit without
# re-running the body, which is the opposite of what the read tests assert.
_read_courses_uncached = courses_module._cached_read_courses.__wrapped__  # type: ignore[attr-defined]
_read_course_uncached = courses_module._cached_read_course.__wrapped__  # type: ignore[attr-defined]


def _current_user() -> dict[str, Any]:
    return {"id": USER_ID, "uuid": USER_UUID, "username": "ada", "is_superuser": False}


def _internal_course(**overrides: Any) -> CourseReadInternal:
    values: dict[str, Any] = {
        "id": 11,
        "user_id": USER_ID,
        "uuid": uuid7(),
        "name": "Advanced Nitrox",
        "agency": "tdi",
        "status": CourseStatus.COMPLETED,
        "start_date": date(2026, 3, 2),
        "end_date": date(2026, 3, 6),
        "notes": "",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    values.update(overrides)
    return CourseReadInternal(**values)


# The four filters at rest. `_cached_read_courses` gives them no defaults on purpose - every
# one is a cache-key dimension, so a caller that forgets one has to say so rather than get an
# unfiltered page under a key claiming it filtered.
_NO_FILTERS: dict[str, Any] = {"date_from": None, "date_to": None, "agency": None, "status": None}


def _get_request() -> MagicMock:
    request = MagicMock()
    request.method = "GET"
    return request


class _FakeRedis:
    """A cache that never hits, recording what the decorator writes and deletes."""

    def __init__(self) -> None:
        self.written: dict[str, str] = {}
        self.deleted: list[str] = []

    async def get(self, key: str) -> None:
        return None

    async def set(self, key: str, value: str) -> None:
        self.written[key] = value

    async def expire(self, key: str, seconds: int) -> None:
        return None

    async def delete(self, key: str) -> None:
        self.deleted.append(key)

    async def scan(self, cursor: int = 0, match: str | None = None, count: int | None = None) -> tuple[int, list[str]]:
        return 0, []


@pytest.fixture
def write_collaborators(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stubs everything `write_course`, `patch_course` and `erase_course` touch."""
    stubs: dict[str, Any] = {
        "owned": AsyncMock(return_value=_internal_course()),
        "create": AsyncMock(return_value=_internal_course()),
        "update": AsyncMock(),
        "delete": AsyncMock(),
        "invalidate_courses": AsyncMock(),
        "invalidate_dives": AsyncMock(),
        "invalidate_certifications": AsyncMock(),
        "redis": _FakeRedis(),
    }

    monkeypatch.setattr(cache_module, "client", stubs["redis"])
    monkeypatch.setattr(courses_module, "_get_owned_course", stubs["owned"])
    monkeypatch.setattr(courses_module.crud_courses, "create", stubs["create"])
    monkeypatch.setattr(courses_module.crud_courses, "update", stubs["update"])
    monkeypatch.setattr(courses_module.crud_courses, "delete", stubs["delete"])
    monkeypatch.setattr(courses_module, "invalidate_course_caches", stubs["invalidate_courses"])
    monkeypatch.setattr(courses_module, "invalidate_dive_caches", stubs["invalidate_dives"])
    monkeypatch.setattr(courses_module, "invalidate_certification_caches", stubs["invalidate_certifications"])
    return stubs


async def _patch(body: dict[str, Any]) -> dict[str, str]:
    return await courses_module.patch_course(
        request=MagicMock(),
        uuid=uuid7(),
        values=CourseUpdate.model_validate(body),
        current_user=_current_user(),
        db=MagicMock(),
    )


class TestCourseSchema:
    """What the whole-object schema refuses, and what it deliberately does not."""

    def test_a_minimal_course_defaults_to_completed(self) -> None:
        """Back-filling history is the common case, and a course that issued a card the
        diver already holds is one that finished."""
        course = CourseCreate.model_validate({"name": "Open Water", "agency": "padi"})

        assert course.status is CourseStatus.COMPLETED
        assert (course.start_date, course.end_date) == (None, None)

    def test_a_course_needs_no_dates_at_all(self) -> None:
        """A `planned` course has none yet, and a referral spans months with fuzzy
        edges."""
        course = CourseCreate.model_validate({"name": "Fundamentals", "agency": "gue", "status": "planned"})

        assert course.start_date is None

    def test_an_end_date_before_the_start_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="end_date must be on or after start_date"):
            CourseCreate.model_validate(
                {
                    "name": "Advanced Nitrox",
                    "agency": "tdi",
                    "start_date": "2026-03-06",
                    "end_date": "2026-03-02",
                }
            )

    def test_a_one_day_course_is_not_a_reversed_range(self) -> None:
        course = CourseCreate.model_validate(
            {
                "name": "Nitrox",
                "agency": "padi",
                "start_date": "2026-03-02",
                "end_date": "2026-03-02",
            }
        )

        assert course.start_date == course.end_date

    def test_a_course_need_not_name_an_agency(self) -> None:
        """A course taught by a private instructor runs under none, and the field is where
        the diver says so - absence, not a sentinel value in the vocabulary."""
        course = CourseCreate.model_validate({"name": "Sidemount Fundamentals"})

        assert course.agency is None
        assert course.agency_other is None

    def test_a_course_with_no_agency_may_not_name_one(self) -> None:
        """`agency_other` names the agency `other` stands for, so with no agency at all
        there is nothing for it to name - the same branch of `validate_agency_pairing` that
        refuses it beside `padi`."""
        with pytest.raises(ValidationError, match="agency_other may only be set"):
            CourseCreate.model_validate({"name": "Sidemount Fundamentals", "agency_other": "NSS-CDS"})

    def test_agency_other_is_required_when_the_agency_is_other(self) -> None:
        with pytest.raises(ValidationError, match="agency_other is required"):
            CourseCreate.model_validate({"name": "Cave 1", "agency": "other"})

    def test_agency_other_alongside_a_named_agency_is_refused(self) -> None:
        """Rejected rather than ignored, so a stored row can never carry both - the same
        rule `CertificationBase` applies, from the same function."""
        with pytest.raises(ValidationError, match="agency_other may only be set"):
            CourseCreate.model_validate({"name": "Cave 1", "agency": "padi", "agency_other": "NSS-CDS"})

    def test_an_unknown_status_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            CourseCreate.model_validate({"name": "Open Water", "agency": "padi", "status": "half-done"})

    def test_a_field_the_api_does_not_have_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            CourseCreate.model_validate({"name": "Open Water", "agency": "padi", "dives": 4})

    def test_the_update_schema_refuses_an_explicit_null_for_a_not_null_column(self) -> None:
        with pytest.raises(ValidationError, match="cannot be null"):
            CourseUpdate.model_validate({"status": None})

    @pytest.mark.parametrize("field", ["start_date", "end_date", "instructor_name", "training_center", "agency"])
    def test_the_update_schema_still_clears_a_nullable_field(self, field: str) -> None:
        """Clearing these is a real edit - an instructor misremembered, a course that
        turned out to be `planned` after all, an agency the course never ran under."""
        values = CourseUpdate.model_validate({field: None})

        assert field in values.model_fields_set
        assert getattr(values, field) is None

    def test_the_update_schema_does_not_range_check_a_lone_date(self) -> None:
        """Deliberate: the other half is in the database, so only the route can judge it.
        A schema that refused this would refuse every legitimate one-date edit."""
        values = CourseUpdate.model_validate({"end_date": "1999-01-01"})

        assert values.end_date == date(1999, 1, 1)


class TestWriteCourse:
    @pytest.mark.asyncio
    async def test_a_duplicate_name_is_allowed(self, write_collaborators: dict[str, Any]) -> None:
        """The divergence from trips, and the reason there is no `course_name_exists`
        helper: a course failed once and retaken later is the same name twice."""
        body = CourseCreate.model_validate({"name": "Advanced Nitrox", "agency": "tdi"})

        await courses_module.write_course(
            request=MagicMock(), course=body, current_user=_current_user(), db=MagicMock()
        )
        await courses_module.write_course(
            request=MagicMock(), course=body, current_user=_current_user(), db=MagicMock()
        )

        assert write_collaborators["create"].await_count == 2

    @pytest.mark.asyncio
    async def test_the_created_course_comes_back_in_its_public_shape(self, write_collaborators: dict[str, Any]) -> None:
        """The internal integer keys are what the create path has in hand, and neither may
        reach the response."""
        body = CourseCreate.model_validate({"name": "Advanced Nitrox", "agency": "tdi"})

        created = await courses_module.write_course(
            request=MagicMock(), course=body, current_user=_current_user(), db=MagicMock()
        )

        assert created.user_uuid == USER_UUID
        assert not hasattr(created, "id")
        assert not hasattr(created, "user_id")
        write_collaborators["invalidate_courses"].assert_awaited_once_with(USER_ID)


class TestPatchCourse:
    """The merged-value checks. `CourseUpdate` sees only what was sent, so both pairings
    are the route's to enforce - and both are silent failures if it does not: an inverted
    range would reach `ck_course_date_range` as a 500-shaped `IntegrityError`, and a
    dangling `agency_other` would simply be stored."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"end_date": "2026-03-01"}, id="end_date-before-the-stored-start"),
            pytest.param({"start_date": "2026-03-07"}, id="start_date-after-the-stored-end"),
            pytest.param({"start_date": "2026-03-07", "end_date": "2026-03-06"}, id="both-sent-reversed"),
        ],
    )
    async def test_a_date_that_crosses_the_stored_one_is_a_422(
        self, write_collaborators: dict[str, Any], body: dict[str, Any]
    ) -> None:
        with pytest.raises(UnprocessableEntityException, match="end_date must be on or after start_date"):
            await _patch(body)

        write_collaborators["update"].assert_not_awaited()
        write_collaborators["invalidate_courses"].assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"end_date": "2026-03-09"}, id="end_date-after-the-stored-start"),
            pytest.param({"end_date": "2026-03-02"}, id="end_date-on-the-stored-start"),
            pytest.param({"start_date": "2026-03-06"}, id="start_date-on-the-stored-end"),
            # Both halves are nullable, so clearing either one can never conflict with
            # whatever is stored on the other.
            pytest.param({"end_date": None}, id="end_date-cleared"),
            pytest.param({"start_date": None}, id="start_date-cleared"),
        ],
    )
    async def test_a_date_that_still_orders_is_written(
        self, write_collaborators: dict[str, Any], body: dict[str, Any]
    ) -> None:
        await _patch(body)

        write_collaborators["update"].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_undated_course_takes_any_date(self, write_collaborators: dict[str, Any]) -> None:
        """Nothing to cross when the stored half is null, so the merge must not read a
        missing date as a reason to refuse."""
        write_collaborators["owned"].return_value = _internal_course(start_date=None, end_date=None)

        await _patch({"end_date": "2030-01-01"})

        write_collaborators["update"].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_edit_that_names_no_date_is_never_range_checked(
        self, write_collaborators: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Keyed off which keys were *sent*, not off the stored row - so a rename cannot be
        refused for a range the caller never touched. Asserted on the check itself rather
        than on the outcome, because a range that still orders would let a
        run-it-every-time route pass this too."""
        checked = MagicMock()
        monkeypatch.setattr(courses_module, "_validate_merged_date_range", checked)

        await _patch({"name": "Advanced Nitrox (retake)"})

        checked.assert_not_called()
        write_collaborators["update"].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_edit_that_names_a_date_is_range_checked(
        self, write_collaborators: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half, so the test above cannot pass by the check having been removed
        altogether - and it pins the merge itself: the stored `start_date` is what the
        incoming `end_date` is judged against."""
        checked = MagicMock()
        monkeypatch.setattr(courses_module, "_validate_merged_date_range", checked)

        await _patch({"end_date": "2026-03-09"})

        checked.assert_called_once_with(date(2026, 3, 2), date(2026, 3, 9))

    @pytest.mark.asyncio
    async def test_moving_to_other_without_naming_the_agency_is_a_422(
        self, write_collaborators: dict[str, Any]
    ) -> None:
        with pytest.raises(UnprocessableEntityException, match="agency_other is required"):
            await _patch({"agency": "other"})

        write_collaborators["update"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_clearing_the_agency_name_while_still_other_is_a_422(
        self, write_collaborators: dict[str, Any]
    ) -> None:
        """The half a whole-object schema cannot see: `agency` stays `other` in the
        database while the name it requires is being cleared."""
        write_collaborators["owned"].return_value = _internal_course(agency="other", agency_other="NSS-CDS")

        with pytest.raises(UnprocessableEntityException, match="agency_other is required"):
            await _patch({"agency_other": None})

        write_collaborators["update"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_naming_an_agency_while_the_stored_one_is_not_other_is_a_422(
        self, write_collaborators: dict[str, Any]
    ) -> None:
        with pytest.raises(UnprocessableEntityException, match="agency_other may only be set"):
            await _patch({"agency_other": "NSS-CDS"})

        write_collaborators["update"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_moving_off_other_clears_the_name_in_the_same_request(
        self, write_collaborators: dict[str, Any]
    ) -> None:
        """Both halves sent together is the way out of `other`, and it has to be allowed
        or a course entered under `other` could never be corrected."""
        write_collaborators["owned"].return_value = _internal_course(agency="other", agency_other="NSS-CDS")

        await _patch({"agency": "padi", "agency_other": None})

        write_collaborators["update"].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clearing_the_agency_is_written(self, write_collaborators: dict[str, Any]) -> None:
        """The column is nullable, so an explicit null is an edit rather than the 500
        `NON_NULLABLE_FIELDS` exists to prevent - a course entered under a default agency
        it never ran under has to be correctable."""
        await _patch({"agency": None})

        write_collaborators["update"].assert_awaited_once()
        assert write_collaborators["update"].await_args.kwargs["object"] == {"agency": None}

    @pytest.mark.asyncio
    async def test_clearing_the_agency_while_a_name_is_stored_is_a_422(
        self, write_collaborators: dict[str, Any]
    ) -> None:
        """The merged check answers this one, not the schema: the stored `agency_other`
        would be left naming an agency the course no longer has."""
        write_collaborators["owned"].return_value = _internal_course(agency="other", agency_other="NSS-CDS")

        with pytest.raises(UnprocessableEntityException, match="agency_other may only be set"):
            await _patch({"agency": None})

        write_collaborators["update"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_clearing_both_halves_together_is_written(self, write_collaborators: dict[str, Any]) -> None:
        """The way out of `other` for a course that turns out to have had no agency, and the
        mirror of moving to a named one."""
        write_collaborators["owned"].return_value = _internal_course(agency="other", agency_other="NSS-CDS")

        await _patch({"agency": None, "agency_other": None})

        write_collaborators["update"].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_empty_patch_writes_nothing(self, write_collaborators: dict[str, Any]) -> None:
        await _patch({})

        write_collaborators["update"].assert_not_awaited()
        write_collaborators["invalidate_courses"].assert_not_awaited()


class TestEraseCourse:
    @pytest.mark.asyncio
    async def test_it_invalidates_all_three_cache_families(self, write_collaborators: dict[str, Any]) -> None:
        """Not just the courses. The FK's `ON DELETE SET NULL` just rewrote every dive and
        every certification that pointed here, so their cached reads name a course fresh
        ones no longer do."""
        await courses_module.erase_course(
            request=MagicMock(), uuid=uuid7(), current_user=_current_user(), db=MagicMock()
        )

        write_collaborators["delete"].assert_awaited_once()
        write_collaborators["invalidate_courses"].assert_awaited_once_with(USER_ID)
        write_collaborators["invalidate_dives"].assert_awaited_once_with(USER_ID)
        write_collaborators["invalidate_certifications"].assert_awaited_once_with(USER_ID)

    @pytest.mark.asyncio
    async def test_a_course_that_is_not_the_callers_is_never_deleted(self, write_collaborators: dict[str, Any]) -> None:
        write_collaborators["owned"].side_effect = NotFoundException("Course not found")

        with pytest.raises(NotFoundException):
            await courses_module.erase_course(
                request=MagicMock(), uuid=uuid7(), current_user=_current_user(), db=MagicMock()
            )

        write_collaborators["delete"].assert_not_awaited()


class TestListOrderingSql:
    """What `_LIST_ORDER` compiles to. `TestListOrderingAgainstPostgres` below says
    whether it puts the rows where a diver expects them."""

    def test_it_asks_for_nulls_last_and_breaks_ties_on_uuid(self) -> None:
        sql = str(
            select(Course.id)
            .order_by(*_LIST_ORDER)
            .compile(dialect=postgresql.dialect(paramstyle="named"), compile_kwargs={"literal_binds": True})
        )

        # `NULLS LAST` spelled out, because Postgres's default for `DESC` is `NULLS FIRST`
        # - which would float every dateless course above the most recent real one *and*
        # be unservable by `ix_course_user_id_start_date`.
        assert "ORDER BY course.start_date DESC NULLS LAST, course.uuid DESC" in sql


class TestReadPath:
    @pytest.mark.asyncio
    async def test_a_course_with_no_agency_reads_back_without_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The read shapes widened with the column. A `CourseRead` that still demanded a
        value would fail `response_model` validation on the row the migration now admits -
        a 500 for whoever is signed in rather than for whoever wrote the migration."""
        rows = [{**_internal_course(id=11, agency=None).model_dump(), "id": 11}]
        monkeypatch.setattr(
            courses_module, "get_courses_page", AsyncMock(return_value={"data": rows, "total_count": 1})
        )

        page = await _read_courses_uncached(
            _get_request(),
            user_id=USER_ID,
            user_uuid=USER_UUID,
            db=MagicMock(),
            page=1,
            items_per_page=10,
            search=None,
            **_NO_FILTERS,
        )

        assert page["data"][0]["agency"] is None
        assert page["data"][0]["name"] == "Advanced Nitrox"

    @pytest.mark.asyncio
    async def test_a_page_is_public_shapes_with_no_internal_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = [
            {**_internal_course(id=11).model_dump(), "id": 11},
            {**_internal_course(id=12, name="Deco Procedures").model_dump(), "id": 12},
        ]
        page_query = AsyncMock(return_value={"data": rows, "total_count": 2})
        monkeypatch.setattr(courses_module, "get_courses_page", page_query)

        page = await _read_courses_uncached(
            _get_request(),
            user_id=USER_ID,
            user_uuid=USER_UUID,
            db=MagicMock(),
            page=1,
            items_per_page=10,
            search=None,
            **_NO_FILTERS,
        )

        assert [course["name"] for course in page["data"]] == ["Advanced Nitrox", "Deco Procedures"]
        assert all("id" not in course and "user_id" not in course for course in page["data"])
        assert page["data"][0]["user_uuid"] == USER_UUID

    @pytest.mark.asyncio
    async def test_the_search_term_reaches_the_query_rather_than_a_second_branch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One query serves both branches here, unlike every other searchable resource -
        `search_multi` cannot express this ordering, so there is no second path for a
        search to take."""
        page_query = AsyncMock(return_value={"data": [], "total_count": 0})
        monkeypatch.setattr(courses_module, "get_courses_page", page_query)

        await _read_courses_uncached(
            _get_request(),
            user_id=USER_ID,
            user_uuid=USER_UUID,
            db=MagicMock(),
            page=1,
            items_per_page=10,
            search="nitrox",
            **_NO_FILTERS,
        )

        assert page_query.await_args is not None
        assert page_query.await_args.kwargs["search"] == "nitrox"

    @pytest.mark.asyncio
    async def test_the_list_key_is_the_one_writes_sweep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Byte-identical to what `OwnedResourceCache.read_list` would have written, and
        inside the `user_{id}_course*` pattern `invalidate_course_caches` sweeps. The
        `:search:` segment is the load-bearing part: without it two different searches on
        the same page serve each other's results."""
        monkeypatch.setattr(courses_module, "get_courses_page", AsyncMock(return_value={"data": [], "total_count": 0}))
        redis = _FakeRedis()

        with patch.object(cache_module, "client", redis):
            await courses_module._cached_read_courses(
                _get_request(),
                user_id=USER_ID,
                user_uuid=USER_UUID,
                db=MagicMock(),
                page=2,
                items_per_page=10,
                search="nitrox",
                **_NO_FILTERS,
            )

        (key,) = redis.written
        assert key == namespaced(
            f"user_{USER_ID}_courses:page_2:items_per_page:10:search:nitrox"
            f":date_from:None:date_to:None:agency:None:status:None:{USER_ID}"
        )
        assert fnmatch(key, across_builds(f"user_{USER_ID}_course*"))

    @pytest.mark.asyncio
    async def test_each_filter_is_a_dimension_of_the_list_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The defect the `:search:` segment exists to prevent, four more times over: a page
        read under one filter must not be served to a request carrying another. Asserted by
        varying one filter at a time and requiring every key to differ - a segment left out
        of the prefix collapses its pair onto one entry."""
        monkeypatch.setattr(courses_module, "get_courses_page", AsyncMock(return_value={"data": [], "total_count": 0}))
        varied: list[dict[str, Any]] = [
            {},
            {"date_from": date(2025, 1, 1)},
            {"date_to": date(2025, 12, 31)},
            {"agency": "tdi"},
            {"status": "planned"},
        ]
        redis = _FakeRedis()

        with patch.object(cache_module, "client", redis):
            for filters in varied:
                await courses_module._cached_read_courses(
                    _get_request(),
                    user_id=USER_ID,
                    user_uuid=USER_UUID,
                    db=MagicMock(),
                    page=1,
                    items_per_page=10,
                    search=None,
                    **{**_NO_FILTERS, **filters},
                )

        assert len(redis.written) == len(varied)
        # And still swept by the one wildcard, the filters sitting after the prefix it matches.
        assert all(fnmatch(key, across_builds(f"user_{USER_ID}_course*")) for key in redis.written)

    @pytest.mark.asyncio
    async def test_the_filters_reach_the_query_as_stored_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The enums are the route's boundary, not the column's: `agency` and `status` are
        `VARCHAR` holding whatever a write put there (see `StoredVocabulary`), so what reaches
        the query - and the cache key - is the plain value rather than the member."""
        cached = AsyncMock(return_value={})
        monkeypatch.setattr(courses_module, "_cached_read_courses", cached)

        await courses_module.read_courses(
            request=MagicMock(),
            current_user=_current_user(),
            db=MagicMock(),
            page=1,
            items_per_page=10,
            search=None,
            date_from=date(2025, 1, 1),
            date_to=date(2025, 12, 31),
            agency=CertificationAgency.TDI,
            status=CourseStatus.PLANNED,
        )

        assert cached.await_args is not None
        assert cached.await_args.kwargs["date_from"] == date(2025, 1, 1)
        assert cached.await_args.kwargs["date_to"] == date(2025, 12, 31)
        assert cached.await_args.kwargs["agency"] == "tdi"
        assert cached.await_args.kwargs["status"] == "planned"

    @pytest.mark.asyncio
    async def test_an_unfiltered_list_passes_every_filter_as_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The other half: absent means absent all the way down, so the unfiltered list keeps
        one cache entry rather than gaining a second spelling of the same page."""
        cached = AsyncMock(return_value={})
        monkeypatch.setattr(courses_module, "_cached_read_courses", cached)

        await courses_module.read_courses(
            request=MagicMock(), current_user=_current_user(), db=MagicMock(), page=1, items_per_page=10
        )

        assert cached.await_args is not None
        assert all(cached.await_args.kwargs[name] is None for name in ("date_from", "date_to", "agency", "status"))

    @pytest.mark.asyncio
    async def test_the_item_key_is_swept_by_the_same_pattern(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A single-course read has to fall under the same wildcard, or an edit would stay
        invisible on the detail page until the entry expired."""
        course = _internal_course()
        monkeypatch.setattr(courses_module.crud_courses, "get", AsyncMock(return_value=course))
        redis = _FakeRedis()

        with patch.object(cache_module, "client", redis):
            await courses_module._cached_read_course(
                _get_request(), user_id=USER_ID, uuid=course.uuid, owner_uuid=USER_UUID, db=MagicMock()
            )

        (key,) = redis.written
        assert key == namespaced(f"user_{USER_ID}_course:{course.uuid}")
        assert fnmatch(key, across_builds(f"user_{USER_ID}_course*"))

    @pytest.mark.asyncio
    async def test_an_oversized_page_is_clamped_before_the_cached_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Clamped rather than rejected, and clamped *before* the key is built - otherwise
        the ceiling would still be the value in the cache key."""
        cached = AsyncMock(return_value={})
        monkeypatch.setattr(courses_module, "_cached_read_courses", cached)

        await courses_module.read_courses(
            request=MagicMock(),
            current_user=_current_user(),
            db=MagicMock(),
            page=1,
            items_per_page=10_000,
            search="  TDI  ",
        )

        assert cached.await_args is not None
        assert cached.await_args.kwargs["items_per_page"] == 100
        # Normalized here rather than in the cache layer, so " TDI " and "tdi" share one entry.
        assert cached.await_args.kwargs["search"] == "tdi"

    @pytest.mark.asyncio
    async def test_the_single_read_authorizes_before_it_reaches_the_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ordering rule `fetch_owned_or_raise` exists to centralize: a `@cache` hit
        replays without re-running the body, so a check made inside the cached helper would
        never run on a hit. Asserted by making the ownership check raise and requiring the
        cached read never to be reached."""
        cached = AsyncMock()
        monkeypatch.setattr(courses_module, "_cached_read_course", cached)
        monkeypatch.setattr(
            courses_module, "_get_owned_course", AsyncMock(side_effect=NotFoundException("Course not found"))
        )

        with pytest.raises(NotFoundException):
            await courses_module.read_course(
                request=MagicMock(), uuid=uuid7(), current_user=_current_user(), db=MagicMock()
            )

        cached.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_owned_course_reaches_the_cached_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The other half, so the test above cannot pass by the route never delegating."""
        cached = AsyncMock(return_value=_internal_course())
        monkeypatch.setattr(courses_module, "_cached_read_course", cached)
        monkeypatch.setattr(courses_module, "_get_owned_course", AsyncMock(return_value=_internal_course()))
        uuid = uuid7()

        await courses_module.read_course(request=MagicMock(), uuid=uuid, current_user=_current_user(), db=MagicMock())

        assert cached.await_args is not None
        assert cached.await_args.kwargs["uuid"] == uuid
        assert cached.await_args.kwargs["owner_uuid"] == USER_UUID

    @pytest.mark.asyncio
    async def test_a_missing_course_is_a_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(courses_module.crud_courses, "get", AsyncMock(return_value=None))

        with patch.object(cache_module, "client", _FakeRedis()), pytest.raises(NotFoundException):
            await _read_course_uncached(
                _get_request(), user_id=USER_ID, uuid=uuid_pkg.UUID(int=0), owner_uuid=USER_UUID, db=MagicMock()
            )


class TestTheCreatePathsRefuseAForeignCourse:
    """Both children resolve `course_uuid` on create, and both must answer 422 for a course
    that is not the caller's - the same answer as for one that does not exist, so a uuid
    that leaked out of somebody else's export stays unprobeable.

    Here rather than beside each route's own tests because the two branches are one
    feature: they call the same `user_id`-scoped resolver and produce the same message, and
    splitting them across two modules is how one of them would later be changed alone.
    """

    @pytest.mark.asyncio
    async def test_the_dive_create_path_refuses_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.app.api.v1 import dives as dives_module
        from src.app.schemas.dive import DiveCreateRequest

        create = AsyncMock()
        monkeypatch.setattr(dives_module, "resolve_course_id_for_user", AsyncMock(return_value=None))
        monkeypatch.setattr(dives_module.crud_dives, "create", create)

        body = DiveCreateRequest.model_validate(
            {
                "dive_number": 1,
                "start_time": "2026-06-01T09:00:00+02:00",
                "duration": 1800,
                "notes": "",
                "course_uuid": str(uuid7()),
            }
        )

        with pytest.raises(UnprocessableEntityException, match="Course not found"):
            await dives_module.write_dive(request=MagicMock(), dive=body, current_user=_current_user(), db=MagicMock())

        create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_certification_create_path_refuses_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.app.api.v1 import certifications as certifications_module
        from src.app.schemas.certification import CertificationCreate

        create = AsyncMock()
        monkeypatch.setattr(certifications_module, "resolve_course_id_for_user", AsyncMock(return_value=None))
        monkeypatch.setattr(certifications_module.crud_certifications, "create", create)

        body = CertificationCreate.model_validate(
            {
                "agency": "tdi",
                "name": "Advanced Nitrox",
                "course_uuid": str(uuid7()),
            }
        )

        with pytest.raises(UnprocessableEntityException, match="Course not found"):
            await certifications_module.write_certification(
                request=MagicMock(), certification=body, current_user=_current_user(), db=MagicMock()
            )

        create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_created_certification_reports_the_course_it_was_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The third producer of `CertificationRead.course_uuid`, and the one the web
        dialog's `onSaved(created)` consumes - so a create that resolved the id but dropped
        the uuid from the response would look fine everywhere except the screen that
        matters."""
        from src.app.api.v1 import certifications as certifications_module
        from src.app.schemas.certification import CertificationCreate, CertificationReadInternal

        course_uuid = uuid7()
        stored = CertificationReadInternal(
            id=5,
            user_id=USER_ID,
            uuid=uuid7(),
            agency="tdi",
            name="Advanced Nitrox",
            course_id=88,
            notes="",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        monkeypatch.setattr(certifications_module, "resolve_course_id_for_user", AsyncMock(return_value=88))
        monkeypatch.setattr(certifications_module.crud_certifications, "create", AsyncMock(return_value=stored))
        monkeypatch.setattr(certifications_module, "invalidate_certification_caches", AsyncMock())

        body = CertificationCreate.model_validate(
            {
                "agency": "tdi",
                "name": "Advanced Nitrox",
                "course_uuid": str(course_uuid),
            }
        )
        created = await certifications_module.write_certification(
            request=MagicMock(), certification=body, current_user=_current_user(), db=MagicMock()
        )

        assert created.course_uuid == course_uuid

    def test_a_vanished_course_is_named_in_the_dives_integrity_message(self) -> None:
        """The other end of the same refusal: the course row can go between the resolve and
        the insert, and `_fk_error_detail` is what turns that into the same sentence rather
        than a raw 500."""
        from sqlalchemy.exc import IntegrityError

        from src.app.api.v1.dives import _fk_error_detail

        error = IntegrityError("insert", {}, Exception('violates foreign key constraint "dive_course_id_fkey"'))

        assert _fk_error_detail(error) == "Course not found."


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestListOrderingAgainstPostgres:
    """The ordering through the real query and a real Postgres. Whether `NULLS LAST` puts
    the dateless courses where a diver expects them is a property of the database, not of
    the SQL we emit."""

    @pytest.mark.asyncio
    async def test_dateless_courses_sort_below_every_dated_one(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        undated_first = create_course(db, diver, start_date=None)
        older = create_course(db, diver, start_date=date(2015, 3, 2))
        undated_second = create_course(db, diver, start_date=None)
        newest = create_course(db, diver, start_date=date(2024, 7, 19))

        page = await get_courses_page(db=async_db, user_id=diver.id, offset=0, limit=10)

        # Dated courses newest first, then the dateless ones - which tie on `start_date`
        # and fall back to `uuid DESC`, i.e. most recently created first.
        assert [row["name"] for row in page["data"]] == [
            newest.name,
            older.name,
            undated_second.name,
            undated_first.name,
        ]

    @pytest.mark.asyncio
    async def test_the_first_page_is_not_all_dateless_courses(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """The user-visible shape a bare `desc()` would produce: a diver with a couple of
        planned courses opens the list and sees only those."""
        for _ in range(3):
            create_course(db, diver, start_date=None)
        newest = create_course(db, diver, start_date=date(2024, 7, 19))

        page = await get_courses_page(db=async_db, user_id=diver.id, offset=0, limit=2)

        assert page["data"][0]["name"] == newest.name
        assert page["total_count"] == 4

    @pytest.mark.asyncio
    async def test_a_search_narrows_within_the_callers_own_courses(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        mine = create_course(db, diver)
        create_course(db, other_diver)

        page = await get_courses_page(db=async_db, user_id=diver.id, offset=0, limit=10, search=mine.name[:12].lower())

        assert [row["name"] for row in page["data"]] == [mine.name]

    @pytest.mark.asyncio
    async def test_a_search_that_matches_nothing_is_an_empty_page(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        create_course(db, diver)

        page = await get_courses_page(db=async_db, user_id=diver.id, offset=0, limit=10, search="zzz-no-such-course")

        assert page["data"] == []
        assert page["total_count"] == 0


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheListFilters:
    """The four filters `GET /courses` takes, through the real query and a real Postgres.

    Here rather than as compiled SQL because what they turn on is NULL semantics - a course
    with one date, or with neither - and whether `end_date IS NULL OR end_date >= ...` puts
    an ongoing course in the window is a property of the database rather than of the clause
    we emit.
    """

    @staticmethod
    async def _names(async_db: AsyncSession, user_id: int, **filters: Any) -> set[str]:
        page = await get_courses_page(db=async_db, user_id=user_id, offset=0, limit=50, **filters)
        return {row["name"] for row in page["data"]}

    @pytest.mark.asyncio
    async def test_a_window_matches_every_course_overlapping_it(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """ "Courses I did in 2025" is an overlap question, not a containment one: the course
        that began in December 2024 and finished in February 2025 is one a diver did in 2025,
        and so is the one that started that June and has not ended."""
        straddling_start = create_course(db, diver, start_date=date(2024, 12, 10), end_date=date(2025, 2, 3))
        ongoing = create_course(db, diver, start_date=date(2025, 6, 1), end_date=None)
        wholly_inside = create_course(db, diver, start_date=date(2025, 4, 1), end_date=date(2025, 4, 5))
        create_course(db, diver, start_date=date(2024, 3, 1), end_date=date(2024, 3, 5))
        create_course(db, diver, start_date=None, end_date=None)

        matched = await self._names(async_db, diver.id, date_from=date(2025, 1, 1), date_to=date(2025, 12, 31))

        assert matched == {straddling_start.name, ongoing.name, wholly_inside.name}

    @pytest.mark.asyncio
    async def test_a_dateless_course_drops_out_under_either_bound_alone(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """The `NULLS LAST` ordering keeps a dateless course at the bottom of an unfiltered
        list; a date filter has to remove it outright, or a diver asking about one year gets
        their `planned` courses back as well. Both bounds pass a NULL on their own - `end_date
        IS NULL` is an ongoing course - so the interval clause is what excludes it."""
        dateless = create_course(db, diver, start_date=None, end_date=None)
        dated = create_course(db, diver, start_date=date(2025, 5, 1), end_date=date(2025, 5, 4))

        assert await self._names(async_db, diver.id) == {dateless.name, dated.name}
        assert await self._names(async_db, diver.id, date_from=date(2025, 1, 1)) == {dated.name}
        assert await self._names(async_db, diver.id, date_to=date(2025, 12, 31)) == {dated.name}

    @pytest.mark.asyncio
    async def test_one_bound_alone_is_the_open_ended_question(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """`date_from` alone is "still running on or after this day" and `date_to` alone is
        "had started by it", so a course with only a start date is reachable from both."""
        old = create_course(db, diver, start_date=date(2019, 1, 7), end_date=date(2019, 1, 11))
        recent = create_course(db, diver, start_date=date(2026, 2, 1), end_date=None)

        assert await self._names(async_db, diver.id, date_from=date(2020, 1, 1)) == {recent.name}
        assert await self._names(async_db, diver.id, date_to=date(2020, 1, 1)) == {old.name}

    @pytest.mark.asyncio
    async def test_an_inverted_window_matches_nothing(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """The case the three overlap clauses do *not* answer on their own: with the bounds
        the wrong way round they still admit every course spanning the gap between them, so
        a diver who swapped the two boxes would get their longest courses back. The explicit
        clause in `_overlap_conditions` is what makes the swap visible as an empty list."""
        create_course(db, diver, start_date=date(2024, 6, 1), end_date=date(2026, 1, 1))

        page = await get_courses_page(
            db=async_db, user_id=diver.id, offset=0, limit=50, date_from=date(2025, 12, 31), date_to=date(2025, 1, 1)
        )

        assert page["data"] == []
        assert page["total_count"] == 0

    @pytest.mark.asyncio
    async def test_agency_and_status_are_exact_matches(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        tdi_planned = create_course(db, diver, agency="tdi", status="planned")
        create_course(db, diver, agency="padi", status="planned")
        create_course(db, diver, agency="tdi", status="completed")
        # A course that ran under no agency at all is matched by neither agency, rather than
        # by every one of them - `agency IS NULL` is not equal to anything.
        create_course(db, diver, agency=None, status="planned")

        assert await self._names(async_db, diver.id, agency="tdi", status="planned") == {tdi_planned.name}

    @pytest.mark.asyncio
    async def test_every_filter_ands_with_the_search_term(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """All five narrow together, which is the whole contract: the web's filter row sits
        beside the name search rather than replacing it."""
        wanted = create_course(
            db, diver, agency="tdi", status="completed", start_date=date(2025, 3, 2), end_date=date(2025, 3, 6)
        )
        # Same name, and each differs from `wanted` in exactly one filtered dimension.
        for overrides in (
            {"agency": "padi"},
            {"status": "planned"},
            {"start_date": date(2021, 3, 2), "end_date": date(2021, 3, 6)},
        ):
            other = create_course(
                db,
                diver,
                **{  # type: ignore[arg-type]
                    "agency": "tdi",
                    "status": "completed",
                    "start_date": date(2025, 3, 2),
                    "end_date": date(2025, 3, 6),
                    **overrides,
                },
            )
            other.name = wanted.name
            db.commit()

        page = await get_courses_page(
            db=async_db,
            user_id=diver.id,
            offset=0,
            limit=50,
            search=wanted.name.lower(),
            date_from=date(2025, 1, 1),
            date_to=date(2025, 12, 31),
            agency="tdi",
            status="completed",
        )

        assert [row["uuid"] for row in page["data"]] == [wanted.uuid]
        assert page["total_count"] == 1

    @pytest.mark.asyncio
    async def test_a_filter_never_reaches_another_divers_courses(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        """The ownership scope survives the new clauses - they are appended to it rather than
        replacing it, and a filter that matched across users would be an IDOR wearing a
        query parameter."""
        create_course(db, other_diver, agency="tdi", status="completed", start_date=date(2025, 5, 1))

        assert await self._names(async_db, diver.id, agency="tdi", date_from=date(2025, 1, 1)) == set()


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheDatabaseKeepsTheDateRange:
    """The backstop under the two route-level checks. CRUDAdmin writes through
    `CourseUpdate`, where a lone date slips past the both-present check, so the constraint
    is the only thing standing between the panel and an inverted range."""

    @pytest.mark.asyncio
    async def test_an_inverted_range_is_refused_by_the_constraint(self, db: Session, diver: User) -> None:
        from sqlalchemy.exc import IntegrityError

        course = create_course(db, diver, start_date=date(2026, 3, 6))
        course.end_date = date(2026, 3, 2)

        with pytest.raises(IntegrityError, match="ck_course_date_range"):
            db.commit()
        db.rollback()

    @pytest.mark.asyncio
    async def test_a_lone_end_date_is_allowed(self, db: Session, diver: User) -> None:
        """SQL NULL semantics make the constraint vacuous when either half is absent,
        which is the wanted behaviour rather than a hole: a course whose start nobody
        recorded is a real row."""
        course = create_course(db, diver, start_date=None)
        course.end_date = date(2026, 3, 2)
        db.commit()

        assert course.start_date is None


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheDatabaseAdmitsACourseWithNoAgency:
    """The column's own half of the change, which only Postgres settles: the suite migrates
    its database from the revisions, so this fails on a schema the revision did not reach."""

    @pytest.mark.asyncio
    async def test_a_course_stores_with_no_agency(self, db: Session, diver: User) -> None:
        course = create_course(db, diver, agency=None)

        assert (course.agency, course.agency_other) == (None, None)
        assert course.id is not None

    @pytest.mark.asyncio
    async def test_a_certification_still_may_not(self, db: Session, diver: User) -> None:
        """The asymmetry, from the side that does not move: `certification.agency` is still
        `NOT NULL`, and a card without one is refused by the database itself."""
        from sqlalchemy.exc import IntegrityError

        card = create_certification(db, diver)
        card.agency = None  # type: ignore[assignment]

        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestDeletingACourseUnlinksItsChildren:
    """The `ON DELETE SET NULL` on both links, which nothing else exercises: a dive and a
    certification on a deleted course have to survive with the reference cleared, not go
    with it."""

    @pytest.mark.asyncio
    async def test_the_dive_and_the_certification_survive_unlinked(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        course = create_course(db, diver)
        # Read out as plain ints while each instance is still fresh: every later commit
        # expires the ones before it, and `expunge_all` below then leaves them detached,
        # where reading an expired attribute raises rather than re-querying.
        course_id, course_uuid = course.id, course.uuid
        dive_id = create_dive(db, diver, course=course).id
        certification_id = create_certification(db, diver, course=course).id

        await crud_courses.delete(db=async_db, uuid=course_uuid)

        # `expunge_all`, not `expire_all`: the deleted `Course` is still in this session's
        # identity map, and expiring it makes the next read try to refresh a row that is
        # gone, which raises instead of answering `None`.
        db.expunge_all()
        assert db.get(Course, course_id) is None
        surviving_dive = db.get(Dive, dive_id)
        surviving_certification = db.get(Certification, certification_id)
        assert surviving_dive is not None and surviving_dive.course_id is None
        assert surviving_certification is not None and surviving_certification.course_id is None

    @pytest.mark.asyncio
    async def test_a_soft_deleted_certification_is_unlinked_too(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """The row is still there to be updated, so the FK fires on it like any other -
        which is correct: a hidden card must not go on naming a course that is gone."""
        course = create_course(db, diver)
        course_uuid = course.uuid
        certification = create_certification(db, diver, course=course)
        certification.is_deleted = True
        db.commit()
        certification_id = certification.id

        await crud_courses.delete(db=async_db, uuid=course_uuid)

        db.expunge_all()
        hidden = db.get(Certification, certification_id)
        assert hidden is not None and hidden.course_id is None


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestCourseUuidLookupScoping:
    """The two translation helpers, both scoped to the caller. Neither is reachable
    cross-user through today's callers, which is exactly why they need a test - a guard
    nothing currently exercises is a guard a future caller can quietly walk around. The
    same reasoning `TestTripUuidLookupScoping` records for the helper this one mirrors."""

    @pytest.mark.asyncio
    async def test_another_divers_course_never_resolves(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        theirs = create_course(db, other_diver)

        assert await resolve_course_id_for_user(async_db, course_uuid=theirs.uuid, user_id=diver.id) is None
        assert await get_course_uuids_by_ids(async_db, course_ids=[theirs.id], user_id=diver.id) == {}

    @pytest.mark.asyncio
    async def test_the_callers_own_course_resolves_both_ways(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """The other half, so the test above cannot pass by the lookups being broken."""
        mine = create_course(db, diver)

        assert await resolve_course_id_for_user(async_db, course_uuid=mine.uuid, user_id=diver.id) == mine.id
        assert await get_course_uuids_by_ids(async_db, course_ids=[mine.id], user_id=diver.id) == {mine.id: mine.uuid}

    @pytest.mark.asyncio
    async def test_no_ids_is_no_query(self, async_db: AsyncSession, diver: User) -> None:
        assert await get_course_uuids_by_ids(async_db, course_ids=[], user_id=diver.id) == {}
