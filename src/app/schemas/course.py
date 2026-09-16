import uuid as uuid_pkg
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..core.schemas import (
    NOTES_MAX_LENGTH,
    PublicUUIDSchema,
    RejectsExplicitNulls,
    StoredVocabulary,
    validate_date_range,
)
from .certification import CertificationAgency, validate_agency_pairing


class CourseStatus(StrEnum):
    """How far a course got.

    A closed vocabulary rather than free text, for the same reason `CertificationAgency`
    is one: it is what lets the UI render a status badge and, later, surface courses that
    are still open. This is the single source of truth for it; it is deliberately *not*
    mirrored by a DB `CHECK` constraint (see DECISIONS.md).

    The non-obvious four are all real states an agency produces rather than tidiness:
    a booked course exists before its first dive (`PLANNED`), a referral leaves one open
    for twelve months (`IN_PROGRESS`, `INCOMPLETE`), and GUE issues provisional passes
    upgradeable within six months (`PROVISIONAL`). A GUE pass *level* ("tec pass") is a
    note, not a member - adding values here later is additive and cheap, so the list stays
    the states rather than every gradation of them.
    """

    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    PROVISIONAL = "provisional"
    NOT_PASSED = "not_passed"


class CourseBase(BaseModel):
    name: Annotated[
        str,
        Field(min_length=1, max_length=255, examples=["Advanced Nitrox + Decompression Procedures"]),
    ]
    agency: Annotated[
        CertificationAgency | None,
        Field(
            default=None,
            examples=[CertificationAgency.TDI],
            description="Training agency whose syllabus this course ran; absent when it ran under none",
        ),
    ]
    agency_other: Annotated[
        str | None,
        Field(
            default=None,
            max_length=64,
            description="Agency name, required when `agency` is `other` and not allowed otherwise",
        ),
    ]
    # `completed` because back-filling history is the common case: a diver entering the
    # course that issued a card they already hold is entering one that finished.
    status: Annotated[CourseStatus, Field(default=CourseStatus.COMPLETED)]
    start_date: Annotated[date | None, Field(default=None, examples=["2026-03-02"])]
    end_date: Annotated[date | None, Field(default=None, examples=["2026-03-06"])]
    instructor_name: Annotated[str | None, Field(default=None, max_length=255)]
    instructor_number: Annotated[str | None, Field(default=None, max_length=64)]
    training_center: Annotated[
        str | None,
        Field(default=None, max_length=255, description="Dive shop, resort or club that ran the course"),
    ]
    notes: Annotated[str, Field(default="", max_length=NOTES_MAX_LENGTH)]

    @model_validator(mode="after")
    def _check_agency_other(self) -> Self:
        validate_agency_pairing(self.agency, self.agency_other)
        return self

    @model_validator(mode="after")
    def _check_date_range(self) -> Self:
        validate_date_range(self.start_date, self.end_date)
        return self


class CourseRead(CourseBase, PublicUUIDSchema):
    """Public representation of a course, keyed by its opaque `uuid` rather than the
    sequential internal `id` (which is never exposed over the API).
    """

    # Override `CourseBase`'s enums, which stay enums for the writes that base validates.
    # See *"A stored vocabulary is read back as a string"* in DECISIONS.md.
    agency: StoredVocabulary | None = None  # type: ignore[assignment]  # widening a write base's field
    status: StoredVocabulary  # type: ignore[assignment]  # widening a write base's field; see `StoredVocabulary`

    user_uuid: uuid_pkg.UUID
    created_at: datetime


class CourseReadInternal(CourseBase, PublicUUIDSchema):
    """Mirrors the actual `course` table columns (integer PK/FK), for server-side lookups
    only - never returned directly over the API (use `CourseRead` for the public shape,
    which additionally resolves `user_id` to the owning user's `uuid`).
    """

    agency: StoredVocabulary | None = None  # type: ignore[assignment]  # widening a write base's field
    status: StoredVocabulary  # type: ignore[assignment]  # widening a write base's field; see `StoredVocabulary`

    id: int
    user_id: int
    created_at: datetime


class CourseCreate(CourseBase):
    model_config = ConfigDict(extra="forbid")

    user_uuid: Annotated[uuid_pkg.UUID, Field(description="Public id of the user this course belongs to")]


class CourseCreateInternal(CourseBase):
    model_config = ConfigDict(extra="forbid")

    user_id: int


class CourseUpdate(RejectsExplicitNulls):
    """Partial update.

    Unlike `CourseBase` this validates neither the `agency`/`agency_other` pairing nor the
    date ordering: a PATCH may carry either half of either pair alone, so both can only be
    checked against the merged result. `patch_course` does that once it has the stored row
    in hand - and the `ck_course_date_range` constraint stands behind the admin panel,
    which writes through this schema.
    """

    model_config = ConfigDict(extra="forbid")

    # Everything nullable stays off this list, so an explicit null clears it: an
    # instructor misremembered, the dates of a course that turned out to be `planned`
    # after all, and the agency of one that turns out to have run under none are all real
    # edits. `agency_other` in particular is half of moving a course off `agency="other"`.
    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ("name", "status", "notes")

    name: Annotated[str | None, Field(default=None, min_length=1, max_length=255)]
    agency: Annotated[CertificationAgency | None, Field(default=None)]
    agency_other: Annotated[str | None, Field(default=None, max_length=64)]
    status: Annotated[CourseStatus | None, Field(default=None)]
    start_date: Annotated[date | None, Field(default=None)]
    end_date: Annotated[date | None, Field(default=None)]
    instructor_name: Annotated[str | None, Field(default=None, max_length=255)]
    instructor_number: Annotated[str | None, Field(default=None, max_length=64)]
    training_center: Annotated[str | None, Field(default=None, max_length=255)]
    notes: Annotated[str | None, Field(default=None, max_length=NOTES_MAX_LENGTH)]


class CourseUpdateInternal(CourseUpdate):
    updated_at: datetime
