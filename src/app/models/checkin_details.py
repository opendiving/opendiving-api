from datetime import date

from sqlalchemy import Date, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class CheckinDetails(Base):
    """What a dive shop's desk asks a diver for that is one value each: the email they give
    out, a phone and a date of birth. One row per account, absent until the diver saves one.

    Off `user` so that `get_current_user`, which selects every mapped `user` column, does not
    carry it on every request, and so that `email` here is the diver's own while `user.email`
    stays the sign-in address. Every member is nullable: unfilled is the ordinary state.
    Each width is DiveJSON §6.1's bound on the member it travels as, so an import can write
    whatever a conforming document carries.
    """

    __tablename__ = "checkin_details"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), default=None)
    phone: Mapped[str | None] = mapped_column(String(32), default=None)
    date_of_birth: Mapped[date | None] = mapped_column(Date, default=None)


class CheckinEmergencyContact(Base):
    """One emergency contact, `position` 0 the one to call first.

    Keyed on the user rather than on `checkin_details`, so the summary and the export read the
    list without a join, and replaced wholesale by every write that carries the list - a value
    object with no `uuid` and nothing to address one by, the way `trip_part` is. No unique
    constraint: two contacts may share a name.
    """

    __tablename__ = "checkin_emergency_contact"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    # Served by the composite index below, which leads with it.
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32), default=None)
    relationship: Mapped[str | None] = mapped_column(String(64), default=None)

    __table_args__ = (Index("ix_checkin_emergency_contact_user_id_position", "user_id", "position"),)


class CheckinInsurancePolicy(Base):
    """One dive insurance policy, in the diver's order, on `CheckinEmergencyContact`'s terms.

    `notified_stage`/`notified_for` are the (stage, expiry date) `send_renewal_reminders` last
    emailed about for this policy - the pair `certification` keeps per card - and are on no
    schema that crosses the wire. Null is "never sent". A write that replaces the list carries
    the pair over from an old row with the same provider and expiry (`crud_checkin_details`),
    so saving a list leaves a reminder already sent for an unchanged policy sent.
    """

    __tablename__ = "checkin_insurance_policy"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(255))
    number: Mapped[str | None] = mapped_column(String(64), default=None)
    expires_on: Mapped[date | None] = mapped_column(Date, default=None)
    notified_stage: Mapped[str | None] = mapped_column(String(16), default=None)
    notified_for: Mapped[date | None] = mapped_column(Date, default=None)

    __table_args__ = (Index("ix_checkin_insurance_policy_user_id_position", "user_id", "position"),)
