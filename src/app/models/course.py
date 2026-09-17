from datetime import date

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, TimestampMixin


class Course(Base, PublicUUIDMixin, TimestampMixin):
    """A training course a diver took - "TDI Advanced Nitrox + Decompression Procedures,
    Blue Ocean, March 2026" - grouping the dives logged on it and the certifications it
    issued.

    A trip without a location, structurally: dives point at it the same way they point at
    a trip, and certifications gained the first reference they have ever carried. One
    course can yield several certifications (TDI's combined Advanced Nitrox + Deco
    Procedures is one course and two cards); a certification points at **at most one**
    course, because no agency construct puts a single card at the end of two courses.

    Deliberately carries **no `SoftDeleteMixin`**, following the direction trips and dive
    sites took: deleting a course removes the row, and the `ON DELETE SET NULL` on
    `dive.course_id` and `certification.course_id` unlinks it from everything that pointed
    at it. See *"The row goes, and so does everything pointing at it"* in DECISIONS.md.
    """

    __tablename__ = "course"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    # Where the course got to - see `CourseStatus` in `schemas/course.py`, the single
    # source of truth for the vocabulary, and a closed one for the same reason `agency` is:
    # free text here would make a status badge impossible. No DB `CHECK`, as above.
    #
    # No model-level default on purpose, though `CourseBase` defaults it to `completed`:
    # a default here would be a second copy of that choice, in the layer that cannot
    # explain it.
    status: Mapped[str] = mapped_column(String(32))
    # Which agency's syllabus this course ran - see `CertificationAgency` in
    # `schemas/certification.py`, reused rather than duplicated so a course and the cards
    # it issued can never name the same agency two ways. Stored as a plain string with no
    # DB `CHECK`, exactly like `certification.agency`: the Pydantic field already rejects
    # unknown values on every write path, including the admin panel's.
    #
    # Nullable, where `certification.agency` is not - see *"A course may have no agency,
    # and a certification may not"* in DECISIONS.md. It sits after `status` rather than
    # before it because `MappedAsDataclass` generates `__init__` in declaration order and
    # every column with a `default` has to follow every column without one.
    agency: Mapped[str | None] = mapped_column(String(32), default=None)
    # The agency's name when `agency == "other"`, same pairing rule as a certification's -
    # and, a course's agency being optional, unnameable without one.
    agency_other: Mapped[str | None] = mapped_column(String(64), default=None)
    # Both nullable, diverging from `Trip.start_date`: a `planned` course has no dates
    # yet, and a referral course spans months with fuzzy edges. The ordering invariant
    # (`end_date >= start_date`) is in `__table_args__` below.
    start_date: Mapped[date | None] = mapped_column(Date, default=None)
    end_date: Mapped[date | None] = mapped_column(Date, default=None)
    # The same three fields a certification carries, with the same names and lengths
    # (`models/certification.py`), so the two never drift apart in vocabulary. The
    # duplication is deliberate rather than something to normalize away: imported history
    # arrives certification-first, with no course to hang the fields on, so a certification
    # has to stand alone.
    instructor_name: Mapped[str | None] = mapped_column(String(255), default=None)
    instructor_number: Mapped[str | None] = mapped_column(String(64), default=None)
    training_center: Mapped[str | None] = mapped_column(String(255), default=None)
    notes: Mapped[str] = mapped_column(Text, default="")

    @declared_attr.directive
    @classmethod
    def __table_args__(cls) -> tuple:
        return (
            # A stored course never ends before it begins, whichever route wrote it. The
            # schema's both-present check and `patch_course`'s merged-value check produce
            # the friendly 422s; this is the backstop for the admin panel, which writes
            # through `CourseUpdate` and can send one date alone. SQL NULL semantics make
            # the constraint vacuous when either date is absent, which is exactly right -
            # a `planned` course with only an end date is a real state.
            #
            # `trip` carries no equivalent: that is a fact about an existing table, not a
            # precedent. A fresh table gets it for free.
            CheckConstraint("end_date >= start_date", name="ck_course_date_range"),
            # Serves `_cached_read_courses` (`GET /courses`): `WHERE user_id = ... ORDER BY
            # start_date DESC NULLS LAST`. `certification`'s ordering index without the
            # partial `WHERE`, since courses have no `is_deleted`. The null placement is
            # load-bearing on both sides: an index built `NULLS LAST` cannot serve a query
            # that asks for the default `NULLS FIRST`, so the reader spells its own out -
            # see `_LIST_ORDER` in `crud_courses`. The reader's trailing `uuid` tiebreak is
            # deliberately not a third column here; it only orders courses that already
            # share a date, which Postgres sorts incrementally on top of this index.
            #
            # That list's date/agency/status filters ride this index too, as a filter step on
            # rows it is already walking in order. None earns an index of its own: one keyed
            # on `agency` or `status` could not serve the `ORDER BY`, so it would buy a scan
            # and pay for a sort, at a diver's handful of courses.
            Index(
                "ix_course_user_id_start_date",
                "user_id",
                cls.start_date.desc().nullslast(),
            ),
        )

    # Deliberately no unique index on (user_id, lower(name)), diverging from
    # `ux_trip_user_id_name_lower`. A course failed once and retaken later is legitimately
    # the same name twice - the same reasoning that left `certification` without one.
