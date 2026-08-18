from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, TimestampMixin


class GearServiceSchedule(Base, PublicUUIDMixin, TimestampMixin):
    """A servicing *rule* attached to a gear item: "this regulator needs a full service
    every 12 months or every 100 dives, whichever comes first".

    Deliberately separate from `GearServiceRecord` (the history of what was actually
    done). A cylinder needs an annual visual inspection *and* a five-year hydrostatic
    test at the same time, so a single interval per item can't express what divers
    actually track - hence one row per (item, kind, label) rather than columns on
    `gear_item`. Keeping the rule apart from the events also means a diver can set up a
    reminder on brand-new kit that has never been serviced (the baseline is `starts_on`),
    and can change the interval without rewriting history.
    """

    __tablename__ = "gear_service_schedule"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, unique=True, primary_key=True, init=False)
    # Denormalized from `gear_item.user_id`. Three call sites want the owner without a
    # join: the route's ownership check, `invalidate_gear_caches(user_id)`, and the
    # digest job's per-user grouping. Gear never changes hands, so it can't drift.
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    gear_item_id: Mapped[int] = mapped_column(ForeignKey("gear_item.id", ondelete="CASCADE"))
    # What kind of servicing this rule is about - see `ServiceKind` in
    # `schemas/gear_service.py`, which is the single source of truth for the vocabulary.
    # Stored as a plain string with no DB `CHECK`, exactly like `gear_item.type`: the
    # Pydantic field already rejects unknown values on every write (including through
    # the admin panel), so a DB-level copy would buy nothing and would need a DDL change
    # every time a kind is added.
    kind: Mapped[str] = mapped_column(String(32))
    # The baseline the first due date is measured from, used until the item has its
    # first `GearServiceRecord` of this kind - "in service since". For a used cylinder
    # this is typically the date stamped on it; for new kit, the purchase date.
    starts_on: Mapped[date] = mapped_column(Date)

    # Free-text companion to `kind`, and part of the uniqueness key. This is what makes
    # `OTHER` usable more than once on the same item ("bladder check" / "zip wax")
    # without inventing a new enum member for every one-off.
    label: Mapped[str | None] = mapped_column(String(120), default=None)
    # The two interval arms. At least one must be set (see the CheckConstraint below);
    # when both are, whichever threshold trips first wins.
    interval_months: Mapped[int | None] = mapped_column(Integer, default=None)
    interval_dives: Mapped[int | None] = mapped_column(Integer, default=None)
    # The item's lifetime `dive_count` when this rule was created - the dive-count
    # counterpart of `starts_on`. Snapshotted server-side by `write_gear_service_schedule`
    # from the `GearItem` row it already fetched, never accepted from the client.
    dive_count_at_start: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # Pause reminders without losing the rule. Distinct from `gear_item.is_archived`,
    # which silences every schedule on the item at once, and from deleting the rule,
    # which unlinks its service records for good.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    # ---- derived, maintained solely by `services.gear_service.recalculate_service_schedule` ----
    # Never accepted from a caller. `last_service_on` is NULL until the first record
    # exists; each `next_due_*` is NULL when its interval arm isn't in use.
    last_service_on: Mapped[date | None] = mapped_column(Date, default=None)
    next_due_on: Mapped[date | None] = mapped_column(Date, default=None)
    # A *threshold* (`baseline + interval_dives`), not a remaining count. That's what
    # keeps this column independent of the item's live `dive_count`: logging a dive
    # changes the item's counter and therefore the *displayed* status, but never has to
    # write to this table. See DECISIONS.md.
    next_due_at_dive_count: Mapped[int | None] = mapped_column(Integer, default=None)

    # ---- notify state for the digest job (`core.worker.functions.send_gear_service_digests`) ----
    # Together these make a reminder fire once per threshold crossing rather than every
    # single day. The job compares `(notified_stage, notified_for_due_on,
    # notified_for_due_at_dive_count)` against the schedule's current
    # `(status, next_due_on, next_due_at_dive_count)` and only sends when they differ,
    # so moving the due date (by logging a service or editing the interval) re-arms the
    # reminder automatically. `recalculate_service_schedule` clears all four whenever it
    # moves `next_due_*`, which is why the two can't drift apart.
    notified_stage: Mapped[str | None] = mapped_column(String(16), default=None)
    notified_for_due_on: Mapped[date | None] = mapped_column(Date, default=None)
    notified_for_due_at_dive_count: Mapped[int | None] = mapped_column(Integer, default=None)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    @declared_attr.directive
    @classmethod
    def __table_args__(cls) -> tuple:
        return (
            # Unlike `gear_item.type` (a closed vocabulary already enforced by Pydantic),
            # these are genuine domain invariants worth a DB backstop: a schedule with no
            # interval at all can never become due, so it would sit in the table
            # generating nothing forever. Mirrored in `GearServiceScheduleBase`'s
            # validator and in the frontend's Zod schema.
            CheckConstraint(
                "interval_months IS NOT NULL OR interval_dives IS NOT NULL",
                name="ck_gear_service_schedule_has_an_interval",
            ),
            CheckConstraint(
                "interval_months IS NULL OR interval_months > 0",
                name="ck_gear_service_schedule_interval_months_positive",
            ),
            CheckConstraint(
                "interval_dives IS NULL OR interval_dives > 0",
                name="ck_gear_service_schedule_interval_dives_positive",
            ),
            # One rule per (item, kind, label). COALESCE maps a NULL label to '' so two
            # label-less "service" rules on the same item are duplicates, exactly how
            # `ux_gear_item_user_id_brand_name_lower` treats a NULL brand - while still
            # allowing two distinctly-labelled "other" rules. Deleting a rule frees its
            # slot because the row is gone, not because the index looks away.
            #
            # It has to stay *unpartitioned* for a second reason now: it is the only index
            # on `gear_item_id`, which is an `ON DELETE CASCADE` target, and a referential
            # -integrity lookup carries no `WHERE` of its own, so a partial index cannot
            # serve it. See `gear_service_record`, which needs two plain indexes of its own
            # for exactly that reason.
            Index(
                "ux_gear_service_schedule_item_kind_label",
                "gear_item_id",
                "kind",
                func.coalesce(func.lower(cls.label), ""),
                unique=True,
            ),
            # Serves the digest job's `WHERE next_due_on <= today + 30` scan, which runs
            # across *every* user's schedules rather than one owner's - the whole reason
            # `next_due_on` is a stored column instead of being computed in Python.
            # No standalone `gear_item_id` index: it's the leading column of the unique
            # index above, which already fully serves `get_schedules_for_gear_items`'
            # `gear_item_id IN (...)` (same reasoning as `GearSetItem`'s missing one) and
            # the cascade's RI lookup.
            Index(
                "ix_gear_service_schedule_next_due_on",
                "next_due_on",
                postgresql_where=cls.is_active.is_(True),
            ),
        )
