"""The daily totals: the counter's upserts, the two hourly writers, and `GET /admin/stats`.

The writers run against Postgres on a **pinned clock in 2020**, for two reasons. The session
sweep and its snapshot are defined by the day of the clock, and "two sign-outs in different
hours of one day" cannot be staged against the real one. And the suite's database is shared
and never emptied, so a day nobody else writes to is the only day whose count is this
test's alone. The sweep's `DELETE` is unscoped, as the cron runs it; with the clock in
2020 it can reach only the rows these tests put there.

Automatically skipped when no database is reachable - see `CONTRIBUTING.md` for why a run on
the host needs `POSTGRES_SERVER=localhost`.
"""

import asyncio
from collections.abc import AsyncGenerator, Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api import router
from src.app.api.dependencies import get_current_user
from src.app.api.v1.admin import MAX_STATS_DAYS, read_daily_stats
from src.app.core.config import settings
from src.app.core.db.database import async_engine, async_get_db
from src.app.core.exceptions.http_exceptions import UnprocessableEntityException
from src.app.core.setup import create_application
from src.app.core.worker.functions import purge_expired_user_sessions, record_sign_in_totals
from src.app.crud.crud_daily_totals import count_account_created, record_sign_ins
from src.app.crud.crud_user_sessions import live_sessions_for_user
from src.app.models.auth_audit_event import AuthAuditEvent
from src.app.models.daily_total import DailyTotal
from src.app.models.user import User
from src.app.models.user_session import UserSession
from src.app.schemas.auth_audit_event import AuthEventType
from src.app.schemas.daily_total import NO_KEY, DailyMetric
from tests.conftest import db_available
from tests.helpers.generators import create_user

needs_a_database = pytest.mark.skipif(not db_available(), reason="No database connection available")

DAY = date(2020, 1, 15)
_FIRST, _LAST = DAY - timedelta(days=2), DAY + timedelta(days=2)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


@contextmanager
def clock(instant: datetime) -> Iterator[None]:
    """The worker module's `datetime.now` pinned to `instant` - the only clock the sweep and
    its snapshot read."""
    pinned = Mock(now=Mock(return_value=instant))
    with patch("src.app.core.worker.functions.datetime", pinned):
        yield


def _stored(db: Session, day: date, metric: DailyMetric, key: str = NO_KEY) -> int | None:
    db.expire_all()
    row = db.get(DailyTotal, (day, metric.value, key))
    return row.count if row is not None else None


def _session(db: Session, user: User, *, last_used_at: datetime, revoked_at: datetime | None = None) -> int:
    row = UserSession(
        user_id=user.id,
        expires_at=last_used_at + timedelta(days=7),
        ip="203.0.113.7",
        user_agent="TestAgent/1.0",
        revoked_at=revoked_at,
    )
    row.last_used_at = last_used_at
    db.add(row)
    db.commit()
    return row.id


def _exists(db: Session, session_id: int) -> bool:
    db.expire_all()
    return db.get(UserSession, session_id) is not None


@pytest.fixture
def pinned_days(db: Session) -> Generator[None]:
    """Clears the 2020 window before and after: its counts, and the sessions and audit rows
    these tests seeded there."""

    def clear() -> None:
        since, until = at(_FIRST, 0), at(_LAST + timedelta(days=1), 0)
        db.execute(delete(DailyTotal).where(DailyTotal.day >= _FIRST, DailyTotal.day <= _LAST))
        db.execute(delete(UserSession).where(UserSession.last_used_at >= since, UserSession.last_used_at < until))
        db.execute(delete(AuthAuditEvent).where(AuthAuditEvent.created_at >= since, AuthAuditEvent.created_at < until))
        db.commit()

    clear()
    yield
    clear()


@pytest_asyncio.fixture
async def sessions() -> AsyncGenerator[Any]:
    engine = create_async_engine(settings.POSTGRES_ASYNC_PREFIX + settings.POSTGRES_URI)
    yield async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _dispose_the_app_engine() -> AsyncGenerator[None]:
    """The jobs open their own sessions from the app's engine, and a pooled asyncpg
    connection belongs to the loop that opened it - `test_auth_audit.py`'s reason."""
    await async_engine.dispose()
    yield
    await async_engine.dispose()


@needs_a_database
class TestTheCounter:
    @pytest.mark.asyncio
    async def test_two_creations_at_once_both_count(self, db: Session, sessions: Any) -> None:
        """One upsert per creation, so a race between two has no read to lose."""
        key = f"t{uuid7().hex[-10:]}"
        now = datetime.now(UTC)

        async def create() -> None:
            async with sessions() as session:
                await count_account_created(session, source=key, now=now)
                await session.commit()

        try:
            await asyncio.gather(create(), create())

            assert _stored(db, now.date(), DailyMetric.ACCOUNTS_CREATED, key) == 2
        finally:
            db.execute(delete(DailyTotal).where(DailyTotal.key == key))
            db.commit()


@needs_a_database
@pytest.mark.usefixtures("pinned_days")
class TestSignIns:
    @staticmethod
    def _signed_in(db: Session, user: User, when: datetime) -> int:
        row = AuthAuditEvent(
            event_type=AuthEventType.SIGN_IN_SUCCEEDED,
            ip="203.0.113.7",
            user_agent="TestAgent/1.0",
            user_id=user.id,
            created_at=when,
        )
        db.add(row)
        db.commit()
        return row.id

    @pytest.mark.asyncio
    async def test_distinct_accounts_per_utc_day(self, db: Session, sessions: Any) -> None:
        """One account signing in twice is one; the day is UTC's, whatever the hour."""
        first, second = create_user(db), create_user(db)
        self._signed_in(db, first, at(DAY, 1))
        self._signed_in(db, first, at(DAY, 23, 59))
        self._signed_in(db, second, at(DAY, 12))
        self._signed_in(db, second, at(DAY + timedelta(days=1), 0, 1))

        async with sessions() as session:
            await record_sign_ins(session)
            await session.commit()

        assert _stored(db, DAY, DailyMetric.SIGN_INS) == 2
        assert _stored(db, DAY + timedelta(days=1), DailyMetric.SIGN_INS) == 1

    @pytest.mark.asyncio
    async def test_a_day_whose_audit_rows_have_gone_keeps_its_count(self, db: Session) -> None:
        """The audit trail erodes - ninety days, or with an account - and a recount of what
        is left must not lower what was counted while it was all there."""
        first, second = create_user(db), create_user(db)
        self._signed_in(db, first, at(DAY, 9))
        gone = self._signed_in(db, second, at(DAY, 10))

        assert await record_sign_in_totals({}) == "Recorded daily sign-in totals"
        db.execute(delete(AuthAuditEvent).where(AuthAuditEvent.id == gone))
        db.commit()
        await record_sign_in_totals({})

        assert _stored(db, DAY, DailyMetric.SIGN_INS) == 2


@needs_a_database
@pytest.mark.usefixtures("pinned_days")
class TestActiveAccounts:
    """The snapshot inside the session sweep, and the retention that makes it the day's."""

    @pytest.mark.asyncio
    async def test_two_accounts_signing_in_and_out_in_different_hours_both_count(self, db: Session) -> None:
        """The case hourly snapshots over a sweep that deleted sign-outs within the hour got
        wrong: it counted the busiest hour, one account, rather than the day's two."""
        first, second = create_user(db), create_user(db)
        morning = _session(db, first, last_used_at=at(DAY, 9, 10), revoked_at=at(DAY, 9, 40))

        with clock(at(DAY, 10)):
            await purge_expired_user_sessions({})
        assert _stored(db, DAY, DailyMetric.ACTIVE_ACCOUNTS) == 1

        afternoon = _session(db, second, last_used_at=at(DAY, 14, 5), revoked_at=at(DAY, 14, 30))
        with clock(at(DAY, 15)):
            await purge_expired_user_sessions({})
        assert _stored(db, DAY, DailyMetric.ACTIVE_ACCOUNTS) == 2

        with clock(at(DAY + timedelta(days=1), 0)):
            await purge_expired_user_sessions({})
        assert _stored(db, DAY, DailyMetric.ACTIVE_ACCOUNTS) == 2
        assert not _exists(db, morning)
        assert not _exists(db, afternoon)

        with clock(at(DAY + timedelta(days=1), 1)):
            await purge_expired_user_sessions({})
        assert _stored(db, DAY, DailyMetric.ACTIVE_ACCOUNTS) == 2

    @pytest.mark.asyncio
    async def test_a_session_revoked_today_goes_at_the_first_sweep_of_tomorrow(self, db: Session) -> None:
        signed_out = _session(db, create_user(db), last_used_at=at(DAY, 11), revoked_at=at(DAY, 11, 30))

        with clock(at(DAY, 23)):
            await purge_expired_user_sessions({})
        assert _exists(db, signed_out)

        with clock(at(DAY + timedelta(days=1), 0)):
            await purge_expired_user_sessions({})
        assert not _exists(db, signed_out)
        assert _stored(db, DAY, DailyMetric.ACTIVE_ACCOUNTS) == 1

    @pytest.mark.asyncio
    async def test_a_session_revoked_yesterday_is_counted_then_deleted(self, db: Session) -> None:
        """The first sweep of a day that finds one: yesterday's count written, then the row
        gone, in that order and one transaction."""
        yesterday = DAY - timedelta(days=1)
        signed_out = _session(db, create_user(db), last_used_at=at(yesterday, 20), revoked_at=at(yesterday, 21))

        with clock(at(DAY, 1)):
            await purge_expired_user_sessions({})

        assert not _exists(db, signed_out)
        assert _stored(db, yesterday, DailyMetric.ACTIVE_ACCOUNTS) == 1

    @pytest.mark.asyncio
    async def test_a_row_deleted_by_hand_leaves_the_day_counted(self, db: Session) -> None:
        live = _session(db, create_user(db), last_used_at=at(DAY, 10))

        with clock(at(DAY, 11)):
            await purge_expired_user_sessions({})
        db.execute(delete(UserSession).where(UserSession.id == live))
        db.commit()
        with clock(at(DAY, 12)):
            await purge_expired_user_sessions({})

        assert _stored(db, DAY, DailyMetric.ACTIVE_ACCOUNTS) == 1

    @pytest.mark.asyncio
    async def test_the_sessions_list_never_shows_a_kept_revoked_row(self, db: Session, sessions: Any) -> None:
        """The row outlives its revoke by up to a day now, and nothing that lists or
        authenticates may see it: `_live` excludes it as it always did."""
        diver = create_user(db)
        now = datetime.now(UTC)
        kept = _session(db, diver, last_used_at=now, revoked_at=now)
        live = _session(db, diver, last_used_at=now)

        async with sessions() as session:
            listed = {row.id for row in await live_sessions_for_user(session, user_id=diver.id, limit=10)}

        assert listed == {live}
        assert kept not in listed


def _row(day: date, metric: DailyMetric, count: int, key: str = NO_KEY) -> DailyTotal:
    return DailyTotal(day=day, metric=metric.value, key=key, count=count)


@needs_a_database
@pytest.mark.usefixtures("pinned_days")
class TestTheStatsRoute:
    @pytest.mark.asyncio
    async def test_every_day_is_answered_and_the_empty_ones_are_zero(self, db: Session, sessions: Any) -> None:
        db.add_all(
            [
                _row(DAY, DailyMetric.ACCOUNTS_CREATED, 3, "scubaboard"),
                _row(DAY, DailyMetric.ACCOUNTS_CREATED, 1, "waitlist"),
                _row(DAY, DailyMetric.SIGN_INS, 12),
                _row(DAY, DailyMetric.ACTIVE_ACCOUNTS, 20),
                _row(DAY + timedelta(days=1), DailyMetric.ACCOUNTS_CREATED, 2, "retired-forum"),
            ]
        )
        db.commit()

        async with sessions() as session:
            with patch.object(settings, "JOIN_CHANNELS", "scubaboard=ScubaBoard"):
                stats = await read_daily_stats(session, first=DAY - timedelta(days=1), last=DAY + timedelta(days=1))
            accounts = await session.scalar(select(func.count()).select_from(User))

        body = stats.model_dump(mode="json")
        assert body["from"] == "2020-01-14"
        assert body["to"] == "2020-01-16"
        assert body["channels"] == [{"slug": "scubaboard", "label": "ScubaBoard"}]
        assert body["days"] == [
            {"day": "2020-01-14", "accounts_created": {}, "sign_ins": 0, "active_accounts": 0},
            {
                "day": "2020-01-15",
                "accounts_created": {"scubaboard": 3, "waitlist": 1},
                "sign_ins": 12,
                "active_accounts": 20,
            },
            {"day": "2020-01-16", "accounts_created": {"retired-forum": 2}, "sign_ins": 0, "active_accounts": 0},
        ]
        assert body["totals"]["accounts"] == accounts
        assert set(body["totals"]) == {"accounts", "active_now"}

    @pytest.mark.asyncio
    async def test_active_now_counts_accounts_not_sessions(self, db: Session, sessions: Any) -> None:
        """Two live sessions on one account are one; a revoked or expired one is none."""
        async with sessions() as session:
            before = (await read_daily_stats(session, first=DAY, last=DAY)).totals.active_now

        diver = create_user(db)
        now = datetime.now(UTC)
        for _ in range(2):
            _session(db, diver, last_used_at=now)
        _session(db, create_user(db), last_used_at=now, revoked_at=now)
        _session(db, create_user(db), last_used_at=now - timedelta(days=8))

        async with sessions() as session:
            after = (await read_daily_stats(session, first=DAY, last=DAY)).totals.active_now

        assert after == before + 1


class TestTheRange:
    @pytest.mark.asyncio
    async def test_the_longest_range_is_answered(self, mock_db) -> None:
        mock_db.scalar = AsyncMock(return_value=0)
        first = date(2026, 7, 1)
        with (
            patch("src.app.api.v1.admin.daily_totals_between", new_callable=AsyncMock, return_value=[]),
            patch("src.app.api.v1.admin.accounts_with_a_live_session", new_callable=AsyncMock, return_value=0),
        ):
            stats = await read_daily_stats(mock_db, first=first, last=first + timedelta(days=MAX_STATS_DAYS - 1))

        assert len(stats.days) == MAX_STATS_DAYS == 92

    @pytest.mark.asyncio
    async def test_one_day_more_is_a_422(self, mock_db) -> None:
        first = date(2026, 7, 1)
        with pytest.raises(UnprocessableEntityException):
            await read_daily_stats(mock_db, first=first, last=first + timedelta(days=MAX_STATS_DAYS))

    @pytest.mark.asyncio
    async def test_a_range_running_backwards_is_a_422(self, mock_db) -> None:
        with pytest.raises(UnprocessableEntityException):
            await read_daily_stats(mock_db, first=date(2026, 9, 2), last=date(2026, 9, 1))


@pytest.fixture(scope="module")
def stats_app() -> Any:
    """No migrations on start: the gate refuses before any query, and the success case
    stubs the two reads."""
    return create_application(router=router, settings=settings, apply_migrations_on_start=False)


class TestTheGateOverTheWire:
    """The router-level superuser gate, as a client meets it."""

    def test_no_token_is_a_401(self, stats_app: Any) -> None:
        with TestClient(stats_app) as client:
            assert (
                client.get("/api/v1/admin/stats", params={"from": "2026-09-01", "to": "2026-09-30"}).status_code == 401
            )

    def test_a_member_is_a_403(self, stats_app: Any) -> None:
        stats_app.dependency_overrides[get_current_user] = lambda: {"id": 1, "is_superuser": False}
        try:
            with TestClient(stats_app) as client:
                response = client.get("/api/v1/admin/stats", params={"from": "2026-09-01", "to": "2026-09-30"})
        finally:
            stats_app.dependency_overrides = {}

        assert response.status_code == 403

    def test_the_operator_reads_from_and_to_by_those_names(self, stats_app: Any) -> None:
        """`from` is a Python keyword, so the handler's parameters are aliased; the wire
        names both ways are the contract's."""
        stats_app.dependency_overrides[get_current_user] = lambda: {"id": 1, "is_superuser": True}
        stats_app.dependency_overrides[async_get_db] = lambda: Mock(scalar=AsyncMock(return_value=0))
        try:
            with (
                patch("src.app.api.v1.admin.daily_totals_between", new_callable=AsyncMock, return_value=[]),
                patch("src.app.api.v1.admin.accounts_with_a_live_session", new_callable=AsyncMock, return_value=0),
                TestClient(stats_app) as client,
            ):
                response = client.get("/api/v1/admin/stats", params={"from": "2026-09-01", "to": "2026-09-30"})
        finally:
            stats_app.dependency_overrides = {}

        assert response.status_code == 200
        body = response.json()
        assert (body["from"], body["to"], len(body["days"])) == ("2026-09-01", "2026-09-30", 30)
