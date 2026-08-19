import asyncio
import logging
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

import uvloop
from arq.worker import Worker
from sqlalchemy import and_, or_, select, update

from ...models.gear_item import GearItem
from ...models.gear_service_schedule import GearServiceSchedule
from ...models.user import User
from ...schemas.gear_service import ServiceStatus
from ...services.email_service import send_gear_service_digest_email
from ...services.gear_service import (
    SERVICE_DUE_SOON_DAYS,
    SERVICE_DUE_SOON_DIVES,
    service_kind_label,
    service_status,
    should_notify,
)
from ..db.crud_token_blacklist import crud_token_blacklist
from ..db.database import local_session

asyncio.set_event_loop(uvloop.new_event_loop())

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


# -------- background tasks --------
async def purge_expired_tokens(ctx: dict[Any, Any]) -> str:
    """Delete rows from `token_blacklist` whose `expires_at` is in the past.

    Blacklist entries only need to be kept until the token they reference
    would have expired naturally, since an expired JWT is already rejected
    on its own. Without this cleanup the table grows unbounded, as every
    logout/account-deletion inserts new rows and nothing ever removes them.

    UTC-aware, like every other timestamp comparison here: `expires_at` is a
    `DateTime(timezone=True)` column written by `core.security._blacklist_one`, so a
    naive local `datetime.now()` would compare against it off by the host's UTC offset.
    """
    async with local_session() as db:
        now = datetime.now(UTC)
        expired_count = await crud_token_blacklist.count(db, expires_at__lt=now)
        if expired_count == 0:
            logging.info("No expired blacklisted tokens to purge")
            return "No expired tokens to purge"

        await crud_token_blacklist.delete(db, allow_multiple=True, expires_at__lt=now)
        logging.info("Purged %d expired blacklisted token(s)", expired_count)
        return f"Purged {expired_count} expired token(s)"


def _due_text(row: Any, status: ServiceStatus, today: date) -> str:
    """One line of the digest, phrased from whichever interval arm is the urgent one.

    A schedule can have both a date and a dive threshold, and only one of them is
    usually the reason it turned up here - saying "due 1 Mar" about a regulator that has
    actually run out of dives would be worse than useless. So the overdue arm wins when
    the schedule is overdue, and otherwise the calendar is reported if it has a date at
    all (which is the arm divers think in).
    """
    kind = service_kind_label(row.kind)
    detail = f"{kind} ({row.label})" if row.label else kind

    date_overdue = row.next_due_on is not None and today >= row.next_due_on
    dives_remaining = row.next_due_at_dive_count - row.dive_count if row.next_due_at_dive_count is not None else None
    dives_overdue = dives_remaining is not None and dives_remaining <= 0

    if status is ServiceStatus.OVERDUE and dives_overdue and not date_overdue:
        over_by = -dives_remaining  # type: ignore[operator]  # dives_overdue implies not None
        return f"{detail} overdue by {over_by} dive{'' if over_by == 1 else 's'}"

    if date_overdue:
        return f"{detail} overdue since {row.next_due_on:%-d %b %Y}"

    if row.next_due_on is not None:
        return f"{detail} due {row.next_due_on:%-d %b %Y}"

    if dives_remaining is not None:
        return f"{detail} due in {dives_remaining} dive{'' if dives_remaining == 1 else 's'}"

    return detail


async def send_gear_service_digests(ctx: dict[Any, Any]) -> str:
    """Email each user one digest of the gear they need to get serviced.

    Runs daily on a cron (see `core/worker/settings.py`) but sends far less often than
    that: `should_notify` fires once per threshold crossing, so a diver gets one email
    when something enters "due soon", one when it goes overdue, and then only a
    quarterly nudge while it stays overdue.

    Three filters matter. Archived gear is skipped, so retiring a piece of kit silences it
    without the diver having to also pause every rule on it; `is_active` pauses one rule
    without touching the item; and `user.gear_service_emails` is the opt-out. There is no
    liveness filter on the gear halves any more - a deleted item takes its schedules with
    it - and `User.is_deleted` is the one that remains, since users still soft-delete.

    "Today" is UTC - `User` has no timezone column, and at date granularity with a
    30-day lead time being a few hours out either way changes nothing. If that ever
    matters, the upgrade is to run hourly and gate on the offset of the user's most
    recent dive.
    """
    today = datetime.now(UTC).date()
    now = datetime.now(UTC)
    soon = today + timedelta(days=SERVICE_DUE_SOON_DAYS)

    async with local_session() as db:
        rows = (
            await db.execute(
                select(
                    User.id.label("user_id"),
                    User.email,
                    GearServiceSchedule.id.label("schedule_id"),
                    GearServiceSchedule.kind,
                    GearServiceSchedule.label,
                    GearServiceSchedule.next_due_on,
                    GearServiceSchedule.next_due_at_dive_count,
                    GearServiceSchedule.notified_stage,
                    GearServiceSchedule.notified_for_due_on,
                    GearServiceSchedule.notified_for_due_at_dive_count,
                    GearServiceSchedule.notified_at,
                    GearItem.uuid.label("gear_item_uuid"),
                    GearItem.name,
                    GearItem.brand,
                    GearItem.dive_count,
                )
                .join(GearItem, GearItem.id == GearServiceSchedule.gear_item_id)
                .join(User, User.id == GearServiceSchedule.user_id)
                .where(
                    GearServiceSchedule.is_active.is_(True),
                    GearItem.is_archived.is_(False),
                    User.is_deleted.is_(False),
                    User.gear_service_emails.is_(True),
                    # A cheap pre-filter only - `should_notify` below makes the real
                    # decision. The date arm is served by
                    # `ix_gear_service_schedule_next_due_on`; the dive arm compares two
                    # tables and can't be indexed, so this is a scan of the (small)
                    # schedule table. Materializing "dives remaining" onto the row would
                    # fix that, at the cost of writing to every schedule on every dive -
                    # see DECISIONS.md for why that trade wasn't taken.
                    or_(
                        GearServiceSchedule.next_due_on <= soon,
                        and_(
                            GearServiceSchedule.next_due_at_dive_count.is_not(None),
                            GearItem.dive_count >= GearServiceSchedule.next_due_at_dive_count - SERVICE_DUE_SOON_DIVES,
                        ),
                    ),
                )
                .order_by(User.id, GearServiceSchedule.next_due_on.asc().nulls_last())
            )
        ).all()

    by_user: dict[int, dict[str, Any]] = defaultdict(lambda: {"email": "", "lines": [], "marks": []})
    for row in rows:
        status = service_status(
            next_due_on=row.next_due_on,
            next_due_at_dive_count=row.next_due_at_dive_count,
            dive_count=row.dive_count,
            today=today,
        )
        if not should_notify(
            status=status,
            next_due_on=row.next_due_on,
            next_due_at_dive_count=row.next_due_at_dive_count,
            notified_stage=row.notified_stage,
            notified_for_due_on=row.notified_for_due_on,
            notified_for_due_at_dive_count=row.notified_for_due_at_dive_count,
            notified_at=row.notified_at,
            now=now,
        ):
            continue

        bucket = by_user[row.user_id]
        bucket["email"] = row.email
        label = f"{row.brand} {row.name}" if row.brand else row.name
        bucket["lines"].append((label, _due_text(row, status, today), str(row.gear_item_uuid)))
        bucket["marks"].append((row.schedule_id, status.value, row.next_due_on, row.next_due_at_dive_count))

    if not by_user:
        logging.info("No gear service reminders to send")
        return "No gear service reminders to send"

    sent_users = 0
    sent_schedules = 0
    async with local_session() as db:
        for bucket in by_user.values():
            # Send first, mark second. If Resend fails the exception propagates before
            # the mark, so the worst case is a duplicate email tomorrow rather than a
            # reminder that silently never arrives - for gear safety that's the right
            # way round.
            await send_gear_service_digest_email(bucket["email"], bucket["lines"])

            # One executemany for the whole bucket rather than a round trip per schedule
            # - a diver whose entire kit comes due at once was previously N statements.
            # Matches how `services.dive_stats` and `services.gear_stats` write.
            #
            # No `.where()`, and the primary key travels in the dicts under its own name:
            # that is SQLAlchemy's ORM "bulk UPDATE by primary key", which lifts `id` out
            # of each dict for the WHERE clause and SETs the rest. Matching the id through
            # a `bindparam` instead compiles fine and passes a mocked session, but adds
            # *additional* WHERE criteria, which that path refuses to execute at all. →
            # DECISIONS.md, "The digest's mark is an ORM bulk UPDATE by primary key".
            marks = [
                {
                    "id": schedule_id,
                    "notified_stage": stage,
                    "notified_for_due_on": due_on,
                    "notified_for_due_at_dive_count": due_at_dive_count,
                    "notified_at": now,
                }
                for schedule_id, stage, due_on, due_at_dive_count in bucket["marks"]
            ]
            if marks:
                await db.execute(update(GearServiceSchedule), marks)
            await db.commit()
            sent_users += 1
            sent_schedules += len(bucket["lines"])

    logging.info("Sent %d gear service digest(s) covering %d schedule(s)", sent_users, sent_schedules)
    return f"Sent {sent_users} gear service digest(s) covering {sent_schedules} schedule(s)"


# -------- base functions --------
async def startup(ctx: Worker) -> None:
    logging.info("Worker Started")


async def shutdown(ctx: Worker) -> None:
    logging.info("Worker end")
