"""Service-schedule arithmetic: when is a piece of gear next due, and is it due now.

Everything in the top half of this module is deliberately pure - no session, no clock
of its own - so the rules can be tested exhaustively without a database (see
`tests/test_gear_service.py`) and reused from both the API and the digest worker.

`service_status` is a near-line-for-line twin of `serviceStatus` in the web app's
`src/lib/gear-service.ts`. That duplication is deliberate and is the only one in this
feature: status depends on today's date, so it can't be a cached API field (see
`ServiceStatus`' docstring), which leaves the browser deriving it for the UI and this
module deriving it for the digest job, which has no browser. The constants below are
named identically on both sides so one `grep SERVICE_DUE_SOON` finds the pair.
"""

import calendar
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.gear_service_record import GearServiceRecord
from ..models.gear_service_schedule import GearServiceSchedule
from ..schemas.gear_service import ServiceKind, ServiceStatus

# Human-readable names for the service kinds, for the digest email. The web app keeps
# its own copy in `lib/api/gear-service.ts` for the UI; this one exists because the
# digest has no browser to render for it. Most are just the capitalized value, but the
# multi-word ones would read badly if derived.
_SERVICE_KIND_LABELS = {
    ServiceKind.SERVICE: "Service",
    ServiceKind.VISUAL_INSPECTION: "Visual inspection",
    ServiceKind.HYDROSTATIC_TEST: "Hydrostatic test",
    ServiceKind.BATTERY: "Battery",
    ServiceKind.OXYGEN_CLEAN: "Oxygen cleaning",
    ServiceKind.OTHER: "Service",
}


def service_kind_label(kind: str) -> str:
    """Display name for a service kind, tolerating a value this build doesn't know about
    (a database that has grown a new kind shouldn't render as a blank line in an email).
    """
    try:
        return _SERVICE_KIND_LABELS[ServiceKind(kind)]
    except ValueError:
        return kind.replace("_", " ").capitalize()


# How far ahead a due date starts showing as "due soon". 30 days is roughly the lead
# time for getting a regulator booked in and back before the next trip.
SERVICE_DUE_SOON_DAYS = 30
# The dive-count equivalent - about one trip's worth of diving.
SERVICE_DUE_SOON_DIVES = 10
# How long the digest waits before nagging again about something that is still overdue.
SERVICE_OVERDUE_RENAG_DAYS = 90


def add_months(start: date, months: int) -> date:
    """Add `months` calendar months to `start`, clamping to the end of the target month.

    Clamping is the whole point: "serviced on 31 August, service again in 6 months"
    has to land on 28 (or 29) February, not overflow into March. `timedelta` can't do
    this at all - months aren't a fixed number of days - and `dateutil.relativedelta`
    isn't worth a dependency for one function.
    """
    total = start.month - 1 + months
    year = start.year + total // 12
    month = total % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def next_due_from(
    *,
    baseline_on: date,
    baseline_dive_count: int,
    interval_months: int | None,
    interval_dives: int | None,
) -> tuple[date | None, int | None]:
    """Compute a schedule's `(next_due_on, next_due_at_dive_count)` from its baseline.

    Each arm is independent and returns `None` when its interval isn't in use, so a
    time-only rule has no dive threshold and vice versa. When both are set they're two
    separate thresholds and whichever trips first wins - that comparison happens in
    `service_status`, not here.

    `next_due_at_dive_count` is an *absolute threshold* rather than a remaining count,
    which is what keeps it independent of the item's live `dive_count`: logging a dive
    changes what the status displays as, but never requires a write to this table.
    """
    next_due_on = add_months(baseline_on, interval_months) if interval_months is not None else None
    next_due_at_dive_count = baseline_dive_count + interval_dives if interval_dives is not None else None
    return next_due_on, next_due_at_dive_count


def service_status(
    *,
    next_due_on: date | None,
    next_due_at_dive_count: int | None,
    dive_count: int,
    today: date,
) -> ServiceStatus:
    """How urgent a schedule is, given today's date and the item's live dive count.

    Both arms are evaluated and the more urgent verdict wins, which is what implements
    "annually *or* every 100 dives, whichever comes first". A schedule with neither
    threshold set is `OK` - it can't become due (and the DB `CheckConstraint` means it
    shouldn't exist in the first place).
    """
    statuses = []

    if next_due_on is not None:
        if today >= next_due_on:
            statuses.append(ServiceStatus.OVERDUE)
        elif (next_due_on - today).days <= SERVICE_DUE_SOON_DAYS:
            statuses.append(ServiceStatus.DUE_SOON)

    if next_due_at_dive_count is not None:
        if dive_count >= next_due_at_dive_count:
            statuses.append(ServiceStatus.OVERDUE)
        elif next_due_at_dive_count - dive_count <= SERVICE_DUE_SOON_DIVES:
            statuses.append(ServiceStatus.DUE_SOON)

    if ServiceStatus.OVERDUE in statuses:
        return ServiceStatus.OVERDUE
    if ServiceStatus.DUE_SOON in statuses:
        return ServiceStatus.DUE_SOON
    return ServiceStatus.OK


def dives_since(*, dive_count: int, baseline_dive_count: int) -> int:
    """How many dives an item has done since a given baseline snapshot, floored at zero.

    The floor matters: `gear_item.dive_count` is a *lifetime* counter, so deleting dives
    drives it back down and a raw subtraction can go negative. "-3 dives since service"
    is nonsense to show and worse to compare against a threshold.
    """
    return max(0, dive_count - baseline_dive_count)


def should_notify(
    *,
    status: ServiceStatus,
    next_due_on: date | None,
    next_due_at_dive_count: int | None,
    notified_stage: str | None,
    notified_for_due_on: date | None,
    notified_for_due_at_dive_count: int | None,
    notified_at: datetime | None,
    now: datetime,
) -> bool:
    """Whether the digest should include this schedule on this run.

    The rule is "fire once per threshold crossing, not once per day": send only when the
    schedule's current `(status, next_due_on, next_due_at_dive_count)` differs from what
    was last notified about. That single comparison covers every case worth covering:

    - entering `due_soon`, then later entering `overdue`, each fire exactly once;
    - logging a service moves `next_due_*` and clears the notify state (see
      `recalculate_service_schedule`), which re-arms the reminder for the next cycle;
    - editing an interval moves the due date, so the stored tuple no longer matches and
      the diver is told about the new one;
    - a dive-based interval tripping fires even though nothing wrote to the schedule,
      because `status` is recomputed against the item's live `dive_count` every run.

    The one exception is a schedule that stays `overdue` indefinitely: the tuple never
    changes, so it would go silent forever. `SERVICE_OVERDUE_RENAG_DAYS` re-sends
    quarterly, which is often enough to matter and rare enough not to be ignored.
    """
    if status is ServiceStatus.OK:
        return False

    if (notified_stage, notified_for_due_on, notified_for_due_at_dive_count) != (
        status.value,
        next_due_on,
        next_due_at_dive_count,
    ):
        return True

    if status is ServiceStatus.OVERDUE and notified_at is not None:
        return now - notified_at >= timedelta(days=SERVICE_OVERDUE_RENAG_DAYS)

    return False


async def recalculate_service_schedule(db: AsyncSession, schedule_id: int, commit: bool = True) -> None:
    """Recompute a schedule's derived due fields from its latest service record.

    Recomputing from scratch rather than adjusting incrementally keeps this immune to
    drift, the same approach as `services.gear_stats.recalculate_gear_dive_counts` and
    `services.dive_stats.recalculate_dive_stats`. Call it after any write that can move
    the baseline: creating, editing or deleting the schedule's interval/`starts_on`, and
    creating, editing or deleting one of its records.

    The baseline is the latest non-deleted record for this schedule, falling back to the
    schedule's own `starts_on`/`dive_count_at_start` when it has never been serviced.
    Records are ordered by `(serviced_on DESC, id DESC)` - the `id` tie-break matters
    because two services entered for the same day would otherwise pick arbitrarily.

    The notify state is cleared in the *same* statement that moves `next_due_*`. Doing
    both together is what guarantees they can't drift: a reminder is always armed for
    whatever due date is currently stored, never for a superseded one.
    """
    schedule = (
        await db.execute(select(GearServiceSchedule).where(GearServiceSchedule.id == schedule_id))
    ).scalar_one_or_none()
    if schedule is None:
        return

    latest = (
        await db.execute(
            select(GearServiceRecord.serviced_on, GearServiceRecord.dive_count_at_service)
            .where(
                GearServiceRecord.gear_service_schedule_id == schedule_id,
                GearServiceRecord.is_deleted.is_(False),
            )
            .order_by(GearServiceRecord.serviced_on.desc(), GearServiceRecord.id.desc())
            .limit(1)
        )
    ).first()

    if latest is not None:
        last_service_on: date | None = latest.serviced_on
        baseline_on = latest.serviced_on
        baseline_dive_count = latest.dive_count_at_service
    else:
        last_service_on = None
        baseline_on = schedule.starts_on
        baseline_dive_count = schedule.dive_count_at_start

    next_due_on, next_due_at_dive_count = next_due_from(
        baseline_on=baseline_on,
        baseline_dive_count=baseline_dive_count,
        interval_months=schedule.interval_months,
        interval_dives=schedule.interval_dives,
    )

    await db.execute(
        update(GearServiceSchedule)
        .where(GearServiceSchedule.id == schedule_id)
        .values(
            last_service_on=last_service_on,
            next_due_on=next_due_on,
            next_due_at_dive_count=next_due_at_dive_count,
            notified_stage=None,
            notified_for_due_on=None,
            notified_for_due_at_dive_count=None,
            notified_at=None,
        )
    )

    if commit:
        await db.commit()


async def soft_delete_schedules_for_gear_item(db: AsyncSession, gear_item_id: int, commit: bool = True) -> None:
    """Soft-delete every schedule attached to a gear item, when the item itself is
    soft-deleted.

    Without this the digest would keep emailing about gear the diver can no longer see:
    `is_deleted` on `gear_item` is application-level, so the `ON DELETE CASCADE` on
    `gear_service_schedule.gear_item_id` never fires (it only would on a hard delete).

    A raw `UPDATE` rather than `crud_gear_service_schedules.delete(allow_multiple=True)`:
    fastcrud raises `NoResultFound` when zero rows match, and the overwhelmingly common
    case - deleting an item that never had a schedule - matches zero rows. The same
    pitfall is worked around with a `count()` first in `purge_expired_tokens`; here a
    plain `UPDATE` avoids the extra round trip entirely.

    Records are deliberately left alone. They're unreachable once the item is gone, and
    a soft delete is meant to be recoverable - throwing away the service history would
    make it a good deal less so.
    """
    await db.execute(
        update(GearServiceSchedule)
        .where(GearServiceSchedule.gear_item_id == gear_item_id, GearServiceSchedule.is_deleted.is_(False))
        .values(is_deleted=True, deleted_at=datetime.now(UTC))
    )

    if commit:
        await db.commit()
