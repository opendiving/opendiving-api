from datetime import date

from sqlalchemy import Date, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, SoftDeleteMixin, TimestampMixin


class GearServiceRecord(Base, PublicUUIDMixin, TimestampMixin, SoftDeleteMixin):
    """One servicing event that actually happened: "this regulator was serviced on
    2026-03-14 at Blue Ocean, 45 EUR".

    The history half of the pair described in `models/gear_service_schedule.py`. The
    latest record for a schedule is what its next due date is measured from, but records
    exist independently of any rule - a diver can log "hydro done" on a cylinder they
    never set a reminder for, and that history must survive the rule being deleted.
    """

    __tablename__ = "gear_service_record"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    # Denormalized owner, same reasoning as on `GearServiceSchedule`.
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    gear_item_id: Mapped[int] = mapped_column(ForeignKey("gear_item.id", ondelete="CASCADE"))
    # Copied from the schedule at write time rather than read back through
    # `gear_service_schedule_id`. Two reasons: a record may have no schedule at all, and
    # "when was this cylinder last visually inspected" has to stay answerable after the
    # rule is deleted (at which point the FK below goes NULL).
    kind: Mapped[str] = mapped_column(String(32))
    serviced_on: Mapped[date] = mapped_column(Date)
    # The item's lifetime `dive_count` at the moment this service happened - the
    # baseline a dive-based interval counts up from. Set server-side by
    # `write_gear_service_record` from the `GearItem` row it already fetched for the
    # ownership check; absent from `GearServiceRecordCreate` (which is `extra="forbid"`),
    # so a client can neither set nor spoof it.
    #
    # Note this is a snapshot of a *lifetime* counter, so back-filling an old dive later
    # inflates "dives since this service" (and deleting dives can push it negative -
    # `service_status` clamps at 0). The exact alternative - counting `dive_gear_item`
    # rows whose dive is newer than `serviced_on` - can't be compared against a stored
    # threshold and would need a per-item aggregate on every read. See DECISIONS.md.
    dive_count_at_service: Mapped[int] = mapped_column(Integer)

    # `ON DELETE SET NULL`, not CASCADE: deleting a servicing *rule* must never delete
    # the receipts. Nullable in its own right too - a record can be logged with no rule
    # to attach it to.
    gear_service_schedule_id: Mapped[int | None] = mapped_column(
        ForeignKey("gear_service_schedule.id", ondelete="SET NULL"), default=None
    )
    label: Mapped[str | None] = mapped_column(String(120), default=None)
    # Who did the work - a technician's name, or "self" for a home battery swap. The shop
    # is `contact_id` below; this stays free text because a person is not a contact.
    performed_by: Mapped[str | None] = mapped_column(String(255), default=None)
    # The shop that did the work. `SET NULL` fires for a soft-deleted record too, whose row
    # is still there to be updated.
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contact.id", ondelete="SET NULL"), default=None)
    notes: Mapped[str] = mapped_column(Text, default="")

    @declared_attr.directive
    @classmethod
    def __table_args__(cls) -> tuple:
        return (
            # Serves the gear detail page's "service history, newest first" list.
            Index(
                "ix_gear_service_record_gear_item_id_serviced_on",
                "gear_item_id",
                cls.serviced_on.desc(),
                postgresql_where=cls.is_deleted.is_(False),
            ),
            # Serves the "latest record for this schedule" lookup that
            # `recalculate_service_schedule` runs after every write, and its batched
            # `DISTINCT ON` sibling.
            Index(
                "ix_gear_service_record_schedule_id_serviced_on",
                "gear_service_schedule_id",
                cls.serviced_on.desc(),
                postgresql_where=cls.is_deleted.is_(False),
            ),
            # The two plain indexes below duplicate the leading columns of the two above,
            # and are not redundant with them: both of those are *partial* on
            # `is_deleted`, and this is the one table in the five-way hard-delete change
            # that keeps its soft-delete flag. Postgres runs a referential-integrity
            # lookup with no predicate of its own, so it cannot prove a partial index
            # covers the rows it needs and will not use one - it seq-scans instead. Both
            # FKs here are cascade targets of a delete a diver can trigger from the UI
            # (`gear_item_id` is `ON DELETE CASCADE`, `gear_service_schedule_id` is
            # `ON DELETE SET NULL`), so that scan would run on every gear-item and every
            # schedule delete. Invisible at a few hundred rows; an incident at ten million.
            Index("ix_gear_service_record_gear_item_id", "gear_item_id"),
            Index("ix_gear_service_record_schedule_id", "gear_service_schedule_id"),
            # Plain for the same reason: deleting a contact is a `SET NULL` lookup here.
            Index("ix_gear_service_record_contact_id", "contact_id"),
        )
