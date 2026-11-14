"""Writing and reading `daily_total`.

**Every write is one atomic upsert on `(day, metric, key)`**, so two account creations in
the same second cannot lose a count and an hourly job racing a restart cannot duplicate a
row. And **a stored count never decreases**: the two recomputed metrics are written as
`GREATEST(stored, computed)`, because what they are computed from erodes - an audit row
leaves at ninety days or with its account, a session row once it is dead - and a recompute
over a day whose rows have gone would otherwise zero a real day.

Days are UTC everywhere: `(timestamp AT TIME ZONE 'UTC')::date` in SQL, whatever the
connection's own `TimeZone` is, so the same instant lands on the same day for every writer
and every reader.
"""

from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import Date, Select, cast, delete, func, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from ..models.auth_audit_event import AuthAuditEvent
from ..models.daily_total import DailyTotal
from ..models.monthly_active_account import MonthlyActiveAccount
from ..models.user_session import UserSession
from ..schemas.auth_audit_event import AuthEventType
from ..schemas.daily_total import NO_KEY, DailyMetric

_IDENTITY = [DailyTotal.day, DailyTotal.metric, DailyTotal.key]


def utc_day(column: ColumnElement[datetime] | InstrumentedAttribute[datetime]) -> ColumnElement[date]:
    """The UTC calendar day of a `timestamptz` column."""
    return cast(func.timezone("UTC", column), Date)


def utc_today(now: datetime) -> date:
    return now.astimezone(UTC).date()


async def count_account_created(db: AsyncSession, *, source: str, now: datetime) -> None:
    """Add one to today's `accounts_created` under `source`. Commits nothing - it runs
    inside the transaction that creates the account, so the two land or roll back together.
    """
    statement = insert(DailyTotal).values(day=utc_today(now), metric=DailyMetric.ACCOUNTS_CREATED, key=source, count=1)
    await db.execute(statement.on_conflict_do_update(index_elements=_IDENTITY, set_={"count": DailyTotal.count + 1}))


async def _raise_to(db: AsyncSession, computed: Select) -> None:
    """Upsert `computed`'s `(day, metric, key, count)` rows, never lowering a stored count."""
    statement = insert(DailyTotal).from_select(["day", "metric", "key", "count"], computed)
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=_IDENTITY,
            set_={"count": func.greatest(DailyTotal.count, statement.excluded.count)},
        )
    )


async def record_sign_ins(db: AsyncSession) -> None:
    """Recompute `sign_ins` for every day the audit trail still covers.

    Distinct accounts, not events: one diver signing in on three devices is one. Unbounded
    by date because the audit table's own retention is the bound - whatever it still holds
    is exactly the stretch this can say anything about. Commits nothing.
    """
    day = utc_day(AuthAuditEvent.created_at).label("day")
    await _raise_to(
        db,
        select(
            day,
            literal(DailyMetric.SIGN_INS.value),
            literal(NO_KEY),
            func.count(AuthAuditEvent.user_id.distinct()),
        )
        .where(AuthAuditEvent.event_type == AuthEventType.SIGN_IN_SUCCEEDED, AuthAuditEvent.user_id.is_not(None))
        .group_by(day),
    )


async def snapshot_active_accounts(db: AsyncSession, *, now: datetime) -> None:
    """Record `active_accounts` for today and yesterday (UTC) from the sessions as they are.

    Every session whose `last_used_at` falls on the day counts, revoked or not - a diver who
    signed in and out again was active. Yesterday as well as today because the first run
    after midnight is the last chance to count yesterday's revoked rows: the sweep this runs
    inside keeps a revoked row until the day of its last use has closed, then deletes it.
    Commits nothing.
    """
    today = utc_today(now)
    since = datetime.combine(today - timedelta(days=1), time.min, tzinfo=UTC)
    until = datetime.combine(today + timedelta(days=1), time.min, tzinfo=UTC)
    day = utc_day(UserSession.last_used_at).label("day")
    await _raise_to(
        db,
        select(
            day,
            literal(DailyMetric.ACTIVE_ACCOUNTS.value),
            literal(NO_KEY),
            func.count(UserSession.user_id.distinct()),
        )
        .where(UserSession.last_used_at >= since, UserSession.last_used_at < until)
        .group_by(day),
    )


def month_start(day: date) -> date:
    return day.replace(day=1)


async def record_monthly_active_accounts(db: AsyncSession, *, now: datetime) -> None:
    """Add this month's active accounts to the month's id set, raise the month's
    `monthly_active_accounts` to the set's size, and fold every earlier month into its count.

    Runs beside `snapshot_active_accounts`, before the sweep's `DELETE`, for its reason. It
    reads every session used since the month began, and since yesterday on the 1st, when
    yesterday's revoked rows are still waiting to be counted into the month before. The
    current month is always written, as zero when nobody was active, so a month with no row
    is one this instance never counted. Commits nothing.
    """
    today = utc_today(now)
    current = month_start(today)
    since = datetime.combine(min(current, today - timedelta(days=1)), time.min, tzinfo=UTC)
    until = datetime.combine(today + timedelta(days=1), time.min, tzinfo=UTC)
    month = cast(func.date_trunc("month", func.timezone("UTC", UserSession.last_used_at)), Date)
    added = insert(MonthlyActiveAccount).from_select(
        ["month", "user_id"],
        select(month, UserSession.user_id)
        .where(UserSession.last_used_at >= since, UserSession.last_used_at < until)
        .distinct(),
    )
    await db.execute(added.on_conflict_do_nothing())

    metric = literal(DailyMetric.MONTHLY_ACTIVE_ACCOUNTS.value)
    await _raise_to(
        db,
        select(MonthlyActiveAccount.month, metric, literal(NO_KEY), func.count())
        .where(MonthlyActiveAccount.month < current)
        .group_by(MonthlyActiveAccount.month),
    )
    await _raise_to(
        db,
        select(literal(current, Date), metric, literal(NO_KEY), func.count())
        .select_from(MonthlyActiveAccount)
        .where(MonthlyActiveAccount.month == current),
    )
    await db.execute(delete(MonthlyActiveAccount).where(MonthlyActiveAccount.month < current))


async def monthly_active_accounts_between(db: AsyncSession, *, first: date, last: date) -> dict[date, int]:
    """The stored count of every month from `first`'s to `last`'s, keyed by its first day."""
    rows = await db.execute(
        select(DailyTotal.day, DailyTotal.count).where(
            DailyTotal.metric == DailyMetric.MONTHLY_ACTIVE_ACCOUNTS,
            DailyTotal.key == NO_KEY,
            DailyTotal.day >= month_start(first),
            DailyTotal.day <= last,
        )
    )
    return dict(rows.all())


async def daily_totals_between(db: AsyncSession, *, first: date, last: date) -> list[DailyTotal]:
    """Every stored row whose day falls in `[first, last]`."""
    rows = await db.execute(select(DailyTotal).where(DailyTotal.day >= first, DailyTotal.day <= last))
    return list(rows.scalars())
