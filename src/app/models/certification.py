from datetime import date

from sqlalchemy import Date, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, SoftDeleteMixin, TimestampMixin


class Certification(Base, PublicUUIDMixin, TimestampMixin, SoftDeleteMixin):
    """A diving certification a user holds - "PADI Advanced Open Water Diver, #1234567,
    June 2019" - together with photos or scans of the physical c-card.

    We cannot *issue* certifications the way an agency's own app does; this is a place to
    keep the card itself, so a diver has it on hand at a dive shop without installing
    PADI's or SSI's app alongside this one.

    Deliberately carries **no binary columns**: the card images live in
    `certification_file` (see `models/certification_file.py`), so listing a diver's
    certifications can never drag megabytes of `bytea` through the query.
    """

    __tablename__ = "certification"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    # Which training agency issued this - see `CertificationAgency` in
    # `schemas/certification.py`, which is the single source of truth for the vocabulary.
    # Stored as a plain string with no DB `CHECK`, exactly like `gear_item.type` and
    # `gear_service_schedule.kind`: the Pydantic field already rejects unknown values on
    # every write (including through the admin panel), so a DB-level copy would buy
    # nothing and would need a DDL change every time an agency is added.
    agency: Mapped[str] = mapped_column(String(32))
    # The certification level as printed on the card, e.g. "Advanced Open Water Diver".
    # Free text rather than a per-agency enum: every agency names its levels differently
    # and renames them between syllabus revisions, so a closed vocabulary here would be
    # wrong within a year and would make old cards unenterable.
    #
    # Sits next to `agency` rather than after it in the DDL-natural order because
    # `MappedAsDataclass` generates `__init__` in declaration order: every column with a
    # `default` has to follow every column without one.
    name: Mapped[str] = mapped_column(String(255))
    # The agency's name when `agency == "other"`. There is a long tail of national and
    # regional bodies (VDST, FFESSM, Scuba Schools of the Pacific, ...) that will never
    # justify their own enum member, but whose cards divers still carry.
    agency_other: Mapped[str | None] = mapped_column(String(64), default=None)
    certification_number: Mapped[str | None] = mapped_column(String(64), default=None)
    certified_on: Mapped[date | None] = mapped_column(Date, default=None)
    # Most recreational certifications never expire, but rescue, first-aid/EFR, DAN
    # oxygen provider and most technical cards do - which is exactly the set a diver is
    # most likely to be asked for at a shop.
    expires_on: Mapped[date | None] = mapped_column(Date, default=None)
    instructor_name: Mapped[str | None] = mapped_column(String(255), default=None)
    instructor_number: Mapped[str | None] = mapped_column(String(64), default=None)
    # The dive shop, resort or club that ran the course.
    training_center: Mapped[str | None] = mapped_column(String(255), default=None)
    notes: Mapped[str] = mapped_column(Text, default="")

    @declared_attr.directive
    @classmethod
    def __table_args__(cls) -> tuple:
        return (
            # Serves `read_certifications` (`GET /certifications`): `WHERE user_id = ...
            # AND is_deleted = false ORDER BY certified_on DESC NULLS LAST`. Newest first
            # is the useful order here (unlike gear, which sorts by name) - a diver's most
            # recent certification is the one they are usually being asked to show.
            #
            # The null placement is load-bearing on both sides: an index built `NULLS
            # LAST` cannot serve a query that asks for the default `NULLS FIRST`, so the
            # reader spells its own out - see `_LIST_ORDER` in `crud_certifications`. The
            # reader's trailing `uuid` tiebreak is deliberately not a third column here;
            # it only orders cards that already share a date, which Postgres can sort
            # incrementally on top of this index.
            Index(
                "ix_certification_user_id_certified_on",
                "user_id",
                cls.certified_on.desc().nullslast(),
                postgresql_where=cls.is_deleted.is_(False),
            ),
        )

    # Deliberately no unique index. Unlike gear, a duplicate-looking certification is
    # usually real: divers hold same-named cards from different agencies ("Nitrox" from
    # both PADI and SSI), and re-certify on cards that do expire. Guessing at duplicates
    # here would block legitimate entries to prevent a harmless one.
