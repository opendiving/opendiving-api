"""Unit tests for the Arq worker background tasks."""

from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from botocore.exceptions import ClientError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.core.config import settings
from src.app.core.db.database import async_engine
from src.app.core.worker.functions import (
    AUTHENTICATION_REQUEST_RETENTION,
    INVITATION_RETENTION,
    INVITE_REQUEST_RETENTION,
    _due_text,
    _purge_one_account,
    purge_expired_authentication_requests,
    purge_expired_invitations,
    purge_expired_invite_requests,
    purge_expired_tokens,
    send_gear_service_digests,
    send_renewal_reminders,
    send_year_in_review,
    startup,
)
from src.app.core.worker.settings import WorkerSettings
from src.app.models import Certification, Dive, DiveDiveSite, DiveSpecies
from src.app.models.authentication_request import AuthenticationRequest
from src.app.models.invitation import Invitation
from src.app.models.invite_request import InviteRequest
from src.app.models.user import User
from src.app.schemas.gear_service import ServiceKind, ServiceStatus
from src.app.services.year_in_review import YEAR_IN_REVIEW_BATCH_SIZE, ReviewedDive, YearInReview
from tests.conftest import db_available, unique_email
from tests.helpers.fake_s3 import select_s3_backend
from tests.helpers.generators import (
    create_certification,
    create_dive_site,
    create_gear_item,
    create_gear_service_schedule,
    create_species,
)


class _FakeSessionContext:
    """Minimal async context manager mimicking `local_session()`."""

    def __init__(self, db: AsyncMock) -> None:
        self._db = db

    async def __aenter__(self) -> AsyncMock:
        return self._db

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class TestPurgeExpiredTokens:
    """Test the periodic token_blacklist cleanup job."""

    @pytest.mark.asyncio
    async def test_purge_deletes_expired_rows(self):
        mock_db = AsyncMock()

        with (
            patch("src.app.core.worker.functions.local_session", return_value=_FakeSessionContext(mock_db)),
            patch("src.app.core.worker.functions.crud_token_blacklist") as mock_blacklist,
        ):
            mock_blacklist.count = AsyncMock(return_value=3)
            mock_blacklist.delete = AsyncMock(return_value=None)

            result = await purge_expired_tokens(MagicMock())

            mock_blacklist.count.assert_called_once()
            count_args, count_kwargs = mock_blacklist.count.call_args
            assert count_args[0] is mock_db
            assert isinstance(count_kwargs["expires_at__lt"], datetime)

            mock_blacklist.delete.assert_called_once()
            delete_args, delete_kwargs = mock_blacklist.delete.call_args
            assert delete_args[0] is mock_db
            assert delete_kwargs["allow_multiple"] is True
            assert isinstance(delete_kwargs["expires_at__lt"], datetime)

            assert "3" in result

    @pytest.mark.asyncio
    async def test_purge_skips_delete_when_nothing_expired(self):
        mock_db = AsyncMock()

        with (
            patch("src.app.core.worker.functions.local_session", return_value=_FakeSessionContext(mock_db)),
            patch("src.app.core.worker.functions.crud_token_blacklist") as mock_blacklist,
        ):
            mock_blacklist.count = AsyncMock(return_value=0)
            mock_blacklist.delete = AsyncMock(return_value=None)

            result = await purge_expired_tokens(MagicMock())

            mock_blacklist.delete.assert_not_called()
            assert "No expired" in result


class _DeletingSession(AsyncMock):
    """`local_session()` stand-in that answers one DELETE with a fixed `rowcount`."""

    def __init__(self, rowcount: int) -> None:
        super().__init__()
        self._rowcount = rowcount
        self.statements: list[Any] = []
        self.committed = False

    async def execute(self, statement, parameters=None):
        self.statements.append(statement)
        result = MagicMock()
        result.rowcount = self._rowcount
        return result

    async def commit(self) -> None:
        self.committed = True


class TestPurgeExpiredAuthenticationRequests:
    """The sweep that keeps `authentication_request` - one stored email address per
    sign-in, and nothing that ever deleted one - from growing forever."""

    @pytest.mark.asyncio
    async def test_deletes_rows_past_the_retention_window(self) -> None:
        session = _DeletingSession(4)
        with patch("src.app.core.worker.functions.local_session", return_value=_FakeSessionContext(session)):
            result = await purge_expired_authentication_requests(MagicMock())

        assert len(session.statements) == 1
        compiled = str(session.statements[0])
        assert compiled.strip().upper().startswith("DELETE FROM AUTHENTICATION_REQUEST")
        assert "authentication_request.expires_at <" in compiled
        assert session.committed
        assert "4" in result

    @pytest.mark.asyncio
    async def test_the_cutoff_trails_now_by_the_retention_window(self) -> None:
        """Not `now()`: the row carries the email-change replay leniency
        `verify_email_change` documents, and deleting at expiry would silently end it."""
        session = _DeletingSession(0)
        before = datetime.now(UTC)
        with patch("src.app.core.worker.functions.local_session", return_value=_FakeSessionContext(session)):
            await purge_expired_authentication_requests(MagicMock())

        (cutoff,) = session.statements[0].compile().params.values()
        assert cutoff.tzinfo is not None, "a naive cutoff compares off by the host's UTC offset"
        assert before - AUTHENTICATION_REQUEST_RETENTION - timedelta(minutes=1) <= cutoff
        assert cutoff <= datetime.now(UTC) - AUTHENTICATION_REQUEST_RETENTION

    @pytest.mark.asyncio
    async def test_reports_an_empty_sweep_without_erroring(self) -> None:
        """The common case on an hourly cron, and the reason the sibling job counts before
        it deletes - `rowcount` gets this for free."""
        session = _DeletingSession(0)
        with patch("src.app.core.worker.functions.local_session", return_value=_FakeSessionContext(session)):
            result = await purge_expired_authentication_requests(MagicMock())

        assert "No expired authentication requests" in result


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestPurgeExpiredAuthenticationRequestsAgainstPostgres:
    """The sweep against a real database, because the boundary it has to get right is a
    `WHERE` clause and a mocked session never evaluates one.

    Unscoped, as the cron runs it: it deletes every eligible row in the database, not only
    this test's, so assertions only ever ask after rows the test seeded.
    """

    @pytest_asyncio.fixture(autouse=True)
    async def _dispose_the_app_engine(self) -> AsyncGenerator[None]:
        """Same reason as `TestSendGearServiceDigestsAgainstPostgres` - the job opens its
        own session from the module-level `local_session`, bound to the app's
        session-lifetime engine, and a pooled asyncpg connection belongs to the loop that
        opened it."""
        await async_engine.dispose()
        yield
        await async_engine.dispose()

    @staticmethod
    def _request(db: Session, *, expires_at: datetime, purpose: str = "sign_in") -> AuthenticationRequest:
        row = AuthenticationRequest(
            email=unique_email(),
            token_hash=uuid7().hex,
            expires_at=expires_at,
            purpose=purpose,
        )
        db.add(row)
        db.commit()
        return row

    @staticmethod
    def _still_there(db: Session, row_id: int) -> bool:
        return db.get(AuthenticationRequest, row_id) is not None

    @pytest.mark.asyncio
    async def test_a_long_expired_row_is_deleted(self, db: Session) -> None:
        stale = self._request(db, expires_at=datetime.now(UTC) - AUTHENTICATION_REQUEST_RETENTION - timedelta(days=1))
        stale_id = stale.id
        db.expunge(stale)

        await purge_expired_authentication_requests({})

        assert not self._still_there(db, stale_id)

    @pytest.mark.asyncio
    async def test_a_recently_expired_row_survives_the_retention_window(self, db: Session) -> None:
        """The whole reason the cutoff is not `now()`: `verify_email_change` tolerates a
        replay of an already-used link while it names the account's current address, and
        that leniency lives in this row."""
        recent = self._request(
            db,
            expires_at=datetime.now(UTC) - AUTHENTICATION_REQUEST_RETENTION + timedelta(days=1),
            purpose="email_change",
        )
        recent_id = recent.id
        db.expunge(recent)

        await purge_expired_authentication_requests({})

        assert self._still_there(db, recent_id)

    @pytest.mark.asyncio
    async def test_a_live_request_is_left_alone(self, db: Session) -> None:
        live = self._request(db, expires_at=datetime.now(UTC) + timedelta(minutes=30))
        live_id = live.id
        db.expunge(live)

        await purge_expired_authentication_requests({})

        assert self._still_there(db, live_id)


class TestTheInvitationSweepsAgainstPostgres:
    """The two 90-day sweeps, and the account-purge arm beside them.

    Against a real database for the reason the sweep above it is: what has to be right is a
    `WHERE` clause and a case-folded comparison, and a mocked session evaluates neither.

    Unscoped, as the crons run them: they delete every eligible row in the database, so the
    assertions only ever ask after rows the test seeded, and every seeded row is far outside
    the developer's own data by construction (a fresh `unique_email` each time).
    """

    @pytest_asyncio.fixture(autouse=True)
    async def _dispose_the_app_engine(self) -> AsyncGenerator[None]:
        await async_engine.dispose()
        yield
        await async_engine.dispose()

    @staticmethod
    def _invitation(db: Session, inviter: User, *, age: timedelta, **stamps: Any) -> int:
        row = Invitation(
            email=unique_email(),
            user_id=inviter.id,
            uuid=uuid7(),
            created_at=datetime.now(UTC) - age,
            **stamps,
        )
        db.add(row)
        db.commit()
        row_id = row.id
        db.expunge(row)
        return row_id

    @staticmethod
    def _request_row(db: Session, *, age: timedelta) -> int:
        row = InviteRequest(email=unique_email(), created_at=datetime.now(UTC) - age)
        db.add(row)
        db.commit()
        row_id = row.id
        db.expunge(row)
        return row_id

    @pytest.mark.asyncio
    async def test_an_old_unaccepted_invitation_is_swept(self, db: Session, diver: User) -> None:
        stale = self._invitation(db, diver, age=INVITATION_RETENTION + timedelta(days=1))

        await purge_expired_invitations({})

        assert db.get(Invitation, stale) is None

    @pytest.mark.asyncio
    async def test_a_revoked_invitation_is_swept_on_the_same_clock(self, db: Session, diver: User) -> None:
        """`created_at` rather than `revoked_at`: re-clocking on the revoke would keep a
        withdrawn address around *longer* than a live one, which is backwards."""
        stale = self._invitation(db, diver, age=INVITATION_RETENTION + timedelta(days=1), revoked_at=datetime.now(UTC))

        await purge_expired_invitations({})

        assert db.get(Invitation, stale) is None

    @pytest.mark.asyncio
    async def test_an_accepted_invitation_is_never_swept(self, db: Session, diver: User) -> None:
        """It is two accounts' shared history rather than a pending allow-list entry, and it
        goes when either of them does - not on a clock."""
        accepted = self._invitation(
            db, diver, age=INVITATION_RETENTION * 3, accepted_at=datetime.now(UTC) - INVITATION_RETENTION
        )

        await purge_expired_invitations({})

        assert db.get(Invitation, accepted) is not None

    @pytest.mark.asyncio
    async def test_a_recent_invitation_survives(self, db: Session, diver: User) -> None:
        fresh = self._invitation(db, diver, age=INVITATION_RETENTION - timedelta(days=1))

        await purge_expired_invitations({})

        assert db.get(Invitation, fresh) is not None

    @pytest.mark.asyncio
    async def test_an_old_request_is_swept_and_a_recent_one_is_not(self, db: Session) -> None:
        stale = self._request_row(db, age=INVITE_REQUEST_RETENTION + timedelta(days=1))
        fresh = self._request_row(db, age=INVITE_REQUEST_RETENTION - timedelta(days=1))

        await purge_expired_invite_requests({})

        assert db.get(InviteRequest, stale) is None
        assert db.get(InviteRequest, fresh) is not None

    @pytest.mark.asyncio
    async def test_the_purge_deletes_by_lowercased_address(self, db: Session) -> None:
        """Invariant 13's sharp edge, and the one a mock would answer wrongly rather than
        not at all.

        `_purge_one_account` receives the stored `User.email`, which `POST /auth/complete`
        inserts verbatim from the onboarding token - so a Google-born account's may carry
        capitals, while both invitation tables store lowercase. The two by-address deletes
        beside these compare raw, correctly, because the tables they name hold whatever the
        sign-in path wrote. Comparing raw *here* would leave a purged person's address in
        the allow-list, which is the one thing this arm exists to prevent.
        """
        from tests.helpers.generators import create_user

        purged = create_user(db)
        purged.email = f"Shouty.{purged.id}@Example.COM"
        purged.is_deleted = True
        purged.deleted_at = datetime.now(UTC) - timedelta(days=30)
        db.commit()

        lowered = purged.email.lower()
        inviter = create_user(db)
        invitation = Invitation(email=lowered, user_id=inviter.id, uuid=uuid7())
        request_row = InviteRequest(email=lowered)
        db.add_all([invitation, request_row])
        db.commit()
        invitation_id, request_id, user_id = invitation.id, request_row.id, purged.id
        db.expunge_all()

        engine = create_async_engine(settings.POSTGRES_ASYNC_PREFIX + settings.POSTGRES_URI)
        try:
            async with async_sessionmaker(bind=engine, class_=AsyncSession)() as session:
                gone = await _purge_one_account(
                    session,
                    user_id=user_id,
                    email=f"Shouty.{user_id}@Example.COM",
                    cutoff=datetime.now(UTC),
                )
        finally:
            await engine.dispose()

        assert gone is True
        assert db.get(Invitation, invitation_id) is None
        assert db.get(InviteRequest, request_id) is None

    @pytest.mark.asyncio
    async def test_deleting_an_inviter_cascades_their_invitations(self, db: Session) -> None:
        """The other direction, which needs no statement of its own: `invitation.user_id` is
        the inviter with `ondelete="CASCADE"`, so the hard delete carries their rows down."""
        from sqlalchemy import text

        from tests.helpers.generators import create_user

        inviter = create_user(db)
        row_id = self._invitation(db, inviter, age=timedelta(hours=1))

        db.execute(text('DELETE FROM "user" WHERE id = :id'), {"id": inviter.id})
        db.commit()

        assert db.get(Invitation, row_id) is None


def _row(**overrides):
    """A row as the digest's join query yields it."""
    defaults = {
        "user_id": 1,
        "email": "diver@example.com",
        "schedule_id": 10,
        "kind": ServiceKind.SERVICE.value,
        "label": None,
        "next_due_on": date(2026, 8, 20),
        "next_due_at_dive_count": None,
        "notified_stage": None,
        "notified_for_due_on": None,
        "notified_for_due_at_dive_count": None,
        "notified_at": None,
        "gear_item_uuid": uuid7(),
        "name": "MK25 EVO",
        "brand": "Scubapro",
        "dive_count": 40,
    }
    return SimpleNamespace(**{**defaults, **overrides})


class _RecordingSession(AsyncMock):
    """`local_session()` stand-in that replays a fixed result set and records writes."""

    def __init__(self, rows):
        super().__init__()
        self._rows = rows
        self.updates = []
        self.calls = []

    async def execute(self, statement, parameters=None):
        compiled = str(statement)
        self.calls.append(compiled)
        if compiled.strip().upper().startswith("UPDATE"):
            # The digest marks a whole user's schedules in one executemany, so the values
            # arrive as a list of per-row param dicts alongside the statement rather than
            # baked into it. `updates` stays one entry per schedule either way.
            if parameters is None:
                self.updates.append(statement.compile().params)
            else:
                self.updates.extend(parameters)
            return MagicMock()
        result = MagicMock()
        result.all.return_value = self._rows
        return result


def _patched(session):
    return (
        patch("src.app.core.worker.functions.local_session", return_value=_FakeSessionContext(session)),
        patch("src.app.core.worker.functions.send_gear_service_digest_email", new_callable=AsyncMock),
    )


class TestDueText:
    """Each digest line has to name the arm that actually ran out."""

    TODAY = date(2026, 8, 10)

    def test_reports_an_overdue_date(self) -> None:
        row = _row(next_due_on=date(2026, 7, 1))
        assert "overdue since 1 Jul 2026" in _due_text(row, ServiceStatus.OVERDUE, self.TODAY)

    def test_reports_an_upcoming_date(self) -> None:
        row = _row(next_due_on=date(2026, 8, 20))
        assert "due 20 Aug 2026" in _due_text(row, ServiceStatus.DUE_SOON, self.TODAY)

    def test_reports_the_dive_arm_when_that_is_the_one_that_ran_out(self) -> None:
        # Saying "due 1 Mar" about a regulator that has actually run out of dives would
        # be worse than useless.
        row = _row(next_due_on=date(2027, 3, 1), next_due_at_dive_count=140, dive_count=143)
        text = _due_text(row, ServiceStatus.OVERDUE, self.TODAY)
        assert "overdue by 3 dives" in text
        assert "2027" not in text

    def test_reports_a_dive_only_schedule(self) -> None:
        row = _row(next_due_on=None, next_due_at_dive_count=140, dive_count=135)
        assert "due in 5 dives" in _due_text(row, ServiceStatus.DUE_SOON, self.TODAY)

    def test_singularizes_one_dive(self) -> None:
        row = _row(next_due_on=None, next_due_at_dive_count=140, dive_count=139)
        assert "due in 1 dive" in _due_text(row, ServiceStatus.DUE_SOON, self.TODAY)

    def test_includes_the_kind_and_its_label(self) -> None:
        row = _row(kind=ServiceKind.VISUAL_INSPECTION.value, label="Stage 1")
        assert _due_text(row, ServiceStatus.DUE_SOON, self.TODAY).startswith("Visual inspection (Stage 1)")


class TestSendGearServiceDigests:
    """One email per user, sent before the notify state is marked."""

    @pytest.mark.asyncio
    async def test_sends_nothing_when_no_schedule_is_due(self) -> None:
        session = _RecordingSession([])
        session_patch, email_patch = _patched(session)
        with session_patch, email_patch as send:
            result = await send_gear_service_digests(MagicMock())

        send.assert_not_awaited()
        assert session.updates == []
        assert "No gear service reminders" in result

    @pytest.mark.asyncio
    async def test_sends_one_email_per_user_regardless_of_item_count(self) -> None:
        overdue = date(2020, 1, 1)
        rows = [
            _row(user_id=1, schedule_id=10, next_due_on=overdue, name="MK25 EVO"),
            _row(user_id=1, schedule_id=11, next_due_on=overdue, name="Wing 17L"),
            _row(user_id=2, schedule_id=12, next_due_on=overdue, email="other@example.com", name="AL80"),
        ]
        session = _RecordingSession(rows)
        session_patch, email_patch = _patched(session)
        with session_patch, email_patch as send:
            result = await send_gear_service_digests(MagicMock())

        assert send.await_count == 2
        first_email, first_lines = send.await_args_list[0].args
        assert first_email == "diver@example.com"
        # Both of user 1's items in one list, not two separate emails.
        assert len(first_lines) == 2
        assert {line[0] for line in first_lines} == {"Scubapro MK25 EVO", "Scubapro Wing 17L"}
        assert "2 gear service digest(s) covering 3 schedule(s)" in result

    @pytest.mark.asyncio
    async def test_marks_the_notify_state_after_sending(self) -> None:
        # Send first, mark second: a delivery failure must produce a duplicate tomorrow
        # rather than a reminder that silently never arrives.
        due_on = date(2020, 1, 1)
        session = _RecordingSession([_row(next_due_on=due_on)])
        session_patch, email_patch = _patched(session)
        with session_patch, email_patch:
            await send_gear_service_digests(MagicMock())

        assert len(session.updates) == 1
        marked = session.updates[0]
        assert marked["notified_stage"] == ServiceStatus.OVERDUE.value
        assert marked["notified_for_due_on"] == due_on
        assert isinstance(marked["notified_at"], datetime)

    @pytest.mark.asyncio
    async def test_a_users_schedules_are_marked_in_one_statement(self) -> None:
        """A diver whose whole kit comes due at once should cost one UPDATE, not one per
        item - the same set-based-write rule `dive_stats`/`gear_stats` follow.
        """
        overdue = date(2020, 1, 1)
        rows = [
            _row(user_id=1, schedule_id=10, next_due_on=overdue, name="MK25 EVO"),
            _row(user_id=1, schedule_id=11, next_due_on=overdue, name="Wing 17L"),
            _row(user_id=1, schedule_id=12, next_due_on=overdue, name="AL80"),
        ]
        session = _RecordingSession(rows)
        session_patch, email_patch = _patched(session)
        with session_patch, email_patch:
            await send_gear_service_digests(MagicMock())

        update_statements = [c for c in session.calls if c.strip().upper().startswith("UPDATE")]
        assert len(update_statements) == 1
        # ...while still marking every one of the three schedules.
        assert {mark["id"] for mark in session.updates} == {10, 11, 12}

    @pytest.mark.asyncio
    async def test_a_second_run_with_unchanged_state_sends_nothing(self) -> None:
        due_on = date(2020, 1, 1)
        already = _row(
            next_due_on=due_on,
            notified_stage=ServiceStatus.OVERDUE.value,
            notified_for_due_on=due_on,
            notified_at=datetime.now(UTC),
        )
        session = _RecordingSession([already])
        session_patch, email_patch = _patched(session)
        with session_patch, email_patch as send:
            result = await send_gear_service_digests(MagicMock())

        send.assert_not_awaited()
        assert "No gear service reminders" in result

    @pytest.mark.asyncio
    async def test_a_still_overdue_schedule_re_nags_after_the_quiet_period(self) -> None:
        due_on = date(2020, 1, 1)
        stale = _row(
            next_due_on=due_on,
            notified_stage=ServiceStatus.OVERDUE.value,
            notified_for_due_on=due_on,
            notified_at=datetime.now(UTC) - timedelta(days=120),
        )
        session = _RecordingSession([stale])
        session_patch, email_patch = _patched(session)
        with session_patch, email_patch as send:
            await send_gear_service_digests(MagicMock())

        send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_query_excludes_archived_paused_and_opted_out(self) -> None:
        session = _RecordingSession([])
        session_patch, email_patch = _patched(session)
        with session_patch, email_patch:
            await send_gear_service_digests(MagicMock())

        statement = session.calls[0]
        # Retiring gear must silence it without pausing every rule on it.
        assert "gear_item.is_archived IS false" in statement
        assert "gear_service_schedule.is_active IS true" in statement
        # `User` is the one of the three that still soft-deletes; the gear halves need no
        # clause because a deleted item takes its schedules with it.
        assert '"user".is_deleted IS false' in statement
        assert "gear_item.is_deleted" not in statement
        assert "gear_service_schedule.is_deleted" not in statement
        assert '"user".gear_service_emails IS true' in statement
        # Both interval arms are pre-filtered on.
        assert "gear_service_schedule.next_due_on <=" in statement
        assert "gear_item.dive_count >= gear_service_schedule.next_due_at_dive_count" in statement


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestSendGearServiceDigestsAgainstPostgres:
    """The same job against a real database, because the mocked-session tests above
    cannot see the statement fail.

    `_RecordingSession` accepts any statement and files the parameters away, so an UPDATE
    that Postgres never gets to run reads there as a pass. That is exactly what happened:
    the mark was an ORM executemany UPDATE carrying its own WHERE clause, which SQLAlchemy
    refuses outright ("bulk synchronize of persistent objects not supported..."), so the
    email went out every day and the notify state was never recorded. One test that
    actually reaches the database is the whole guard against that shape of bug.

    Note this exercises `send_gear_service_digests` unscoped, as the cron runs it: it picks
    up every due schedule in the database, not only this test's. Assertions therefore only
    ever look at the row the test seeded.
    """

    @pytest_asyncio.fixture(autouse=True)
    async def _dispose_the_app_engine(self) -> AsyncGenerator[None]:
        """Put the app's own pool in a clean state around each test in this class.

        These tests can't take the `async_db` fixture and its per-test engine, the way
        most of the database-backed suite does: the job under test opens its own session
        from the module-level `local_session`, which is bound to the app's
        session-lifetime `async_engine`. pytest-asyncio gives each test a fresh event
        loop, and a pooled asyncpg connection belongs to the loop that opened it, so
        without this a test is handed a dead-loop connection and dies with "attached to a
        different loop" or "another operation is in progress". Setup and teardown both run
        inside the test's own loop, which is what makes the close legal.

        Both sides, not just teardown: the previous test in this class is not the only
        thing that leaves connections in that pool. The session-scoped `client` fixture
        enters the app's real lifespan on `TestClient`'s portal loop, and the lifespan
        opens the shared engine for its startup work.

        `test_export_loader.py`'s `_load` disposes the same engine for the same reason,
        in a `try/finally` around its own helper. A fixture rather than a helper here
        because the job is called directly, with no wrapper of ours to put the `finally`
        in.
        """
        await async_engine.dispose()
        yield
        await async_engine.dispose()

    @pytest.mark.asyncio
    async def test_the_mark_reaches_postgres_and_writes_the_notify_columns(self, db: Session, diver: User) -> None:
        due_on = date(2020, 1, 1)
        item = create_gear_item(db, diver)
        schedule = create_gear_service_schedule(db, diver, item)
        schedule.next_due_on = due_on
        db.commit()

        with patch("src.app.core.worker.functions.send_gear_service_digest_email", new_callable=AsyncMock) as send:
            await send_gear_service_digests({})

        send.assert_awaited()
        db.refresh(schedule)
        assert schedule.notified_stage == ServiceStatus.OVERDUE.value
        assert schedule.notified_for_due_on == due_on
        assert schedule.notified_at is not None

    @pytest.mark.asyncio
    async def test_a_second_run_does_not_send_the_same_reminder_again(self, db: Session, diver: User) -> None:
        """The consequence of the mark landing, and the reason it matters: without it every
        diver with gear due got the same digest every single day the cron ran."""
        item = create_gear_item(db, diver)
        schedule = create_gear_service_schedule(db, diver, item)
        schedule.next_due_on = date(2020, 1, 1)
        db.commit()

        with patch("src.app.core.worker.functions.send_gear_service_digest_email", new_callable=AsyncMock):
            await send_gear_service_digests({})

        with patch("src.app.core.worker.functions.send_gear_service_digest_email", new_callable=AsyncMock) as send:
            await send_gear_service_digests({})

        assert diver.email not in {call.args[0] for call in send.await_args_list}


TODAY = date(2026, 9, 26)


def _certification_row(**overrides):
    """A row as the renewal job's certification query yields it."""
    defaults = {
        "user_id": 1,
        "email": "diver@example.com",
        "certification_id": 20,
        "agency": "padi",
        "agency_other": None,
        "name": "Rescue Diver",
        "expires_on": TODAY + timedelta(days=30),
        "expiry_notified_stage": None,
        "expiry_notified_for": None,
    }
    return SimpleNamespace(**{**defaults, **overrides})


def _insurance_row(**overrides):
    """A row as the renewal job's insurance query yields it."""
    defaults = {
        "user_id": 1,
        "email": "diver@example.com",
        "insurance_provider": "DAN Europe",
        "insurance_expires_on": TODAY + timedelta(days=10),
        "insurance_notified_stage": None,
        "insurance_notified_for": None,
    }
    return SimpleNamespace(**{**defaults, **overrides})


class _RenewalSession(_RecordingSession):
    """Answers the job's two reads by which table each one is from."""

    def __init__(self, certifications, insurances):
        super().__init__([])
        self._certifications = certifications
        self._insurances = insurances
        self.update_statements = []

    async def execute(self, statement, parameters=None):
        compiled = str(statement)
        if compiled.strip().upper().startswith("UPDATE"):
            self.update_statements.append((statement, parameters))
            return await super().execute(statement, parameters)
        self.calls.append(compiled)
        result = MagicMock()
        result.all.return_value = self._certifications if "FROM certification" in compiled else self._insurances
        return result


def _renewal_patches(session):
    return (
        patch("src.app.core.worker.functions.local_session", return_value=_FakeSessionContext(session)),
        patch("src.app.core.worker.functions.send_renewal_reminder_email", new_callable=AsyncMock),
    )


class TestSendRenewalReminders:
    """One email per diver, cards and insurance together, sent before the pair is marked."""

    @pytest.mark.asyncio
    async def test_sends_nothing_when_nothing_is_running_out(self) -> None:
        session = _RenewalSession([], [])
        session_patch, email_patch = _renewal_patches(session)
        with session_patch, email_patch as send:
            result = await send_renewal_reminders({}, today=TODAY)

        send.assert_not_awaited()
        assert session.update_statements == []
        assert "No renewal reminders" in result

    @pytest.mark.asyncio
    async def test_one_email_per_diver_soonest_first(self) -> None:
        certifications = [
            _certification_row(certification_id=20, name="Rescue Diver", expires_on=TODAY + timedelta(days=60)),
            _certification_row(certification_id=21, name="EFR", agency="efr", expires_on=TODAY - timedelta(days=3)),
            _certification_row(user_id=2, email="other@example.com", certification_id=22),
        ]
        session = _RenewalSession(certifications, [_insurance_row()])
        session_patch, email_patch = _renewal_patches(session)
        with session_patch, email_patch as send:
            result = await send_renewal_reminders({}, today=TODAY)

        assert send.await_count == 2
        email, lines = send.await_args_list[0].args
        assert email == "diver@example.com"
        assert lines == [
            ("EFR EFR", "expired 23 Sep 2026", "/certifications"),
            ("DAN Europe dive insurance", "expires 6 Oct 2026", "/settings"),
            ("PADI Rescue Diver", "expires 25 Nov 2026", "/certifications"),
        ]
        assert "Sent 2 renewal reminder(s) covering 4 subject(s)" in result

    @pytest.mark.asyncio
    async def test_marks_each_subject_after_sending(self) -> None:
        certifications = [
            _certification_row(certification_id=20),
            _certification_row(certification_id=21, expires_on=TODAY - timedelta(days=1)),
        ]
        session = _RenewalSession(certifications, [_insurance_row()])
        session_patch, email_patch = _renewal_patches(session)
        with session_patch, email_patch:
            await send_renewal_reminders({}, today=TODAY)

        # The cards in one executemany by primary key, with no WHERE of its own...
        card_marks = [params for statement, params in session.update_statements if params is not None]
        assert card_marks == [
            [
                {
                    "id": 20,
                    "expiry_notified_stage": "expiring_soon",
                    "expiry_notified_for": TODAY + timedelta(days=30),
                },
                {"id": 21, "expiry_notified_stage": "expired", "expiry_notified_for": TODAY - timedelta(days=1)},
            ]
        ]
        # ...and the insurance on the account's own row.
        [insurance_mark] = [statement for statement, params in session.update_statements if params is None]
        values = insurance_mark.compile().params
        assert values["insurance_notified_stage"] == "expiring_soon"
        assert values["insurance_notified_for"] == TODAY + timedelta(days=10)
        session.commit.assert_awaited()

    @pytest.mark.asyncio
    async def test_a_subject_already_reminded_at_this_stage_sends_nothing(self) -> None:
        expires_on = TODAY + timedelta(days=30)
        card = _certification_row(
            expires_on=expires_on, expiry_notified_stage="expiring_soon", expiry_notified_for=expires_on
        )
        insured = _insurance_row(
            insurance_expires_on=expires_on, insurance_notified_stage="expiring_soon", insurance_notified_for=expires_on
        )
        session = _RenewalSession([card], [insured])
        session_patch, email_patch = _renewal_patches(session)
        with session_patch, email_patch as send:
            await send_renewal_reminders({}, today=TODAY)

        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_queries_exclude_deleted_cards_deleted_accounts_and_the_opted_out(self) -> None:
        session = _RenewalSession([], [])
        session_patch, email_patch = _renewal_patches(session)
        with session_patch, email_patch:
            await send_renewal_reminders({}, today=TODAY)

        certifications, insurances = session.calls
        assert "certification.is_deleted IS false" in certifications
        for statement in (certifications, insurances):
            assert '"user".is_deleted IS false' in statement
            assert '"user".renewal_reminder_emails IS true' in statement
        assert "certification.expires_on <=" in certifications
        assert '"user".insurance_expires_on <=' in insurances


def _review(year: int = 2025) -> YearInReview:
    dive = ReviewedDive(id=1, uuid=uuid7(), day=date(year, 5, 1), max_depth=20.0, duration=2400)
    return YearInReview(
        year=year,
        dives=1,
        seconds_underwater=2400,
        deepest=dive,
        longest=dive,
        dive_sites=0,
        species=0,
        first_species=0,
    )


def _candidate(user_id: int):
    return SimpleNamespace(id=user_id, email=f"diver{user_id}@example.com", units="metric")


def _year_in_review_patches(session, reviews):
    return (
        patch("src.app.core.worker.functions.local_session", return_value=_FakeSessionContext(session)),
        patch("src.app.core.worker.functions.send_year_in_review_email", new_callable=AsyncMock),
        patch("src.app.core.worker.functions.year_in_review", new=AsyncMock(side_effect=reviews)),
    )


class TestSendYearInReview:
    """January only, a batch at a time, oldest accounts first, marked after each send."""

    JANUARY = date(2026, 1, 10)

    @pytest.mark.asyncio
    async def test_does_nothing_outside_january(self) -> None:
        session = _RecordingSession([_candidate(1)])
        session_patch, email_patch, review_patch = _year_in_review_patches(session, [_review()])
        with session_patch, email_patch as send, review_patch:
            result = await send_year_in_review({}, today=date(2026, 2, 1))

        send.assert_not_awaited()
        assert session.calls == []
        assert "outside January" in result

    @pytest.mark.asyncio
    async def test_reviews_the_previous_year_and_marks_it_after_the_send(self) -> None:
        session = _RecordingSession([_candidate(1)])
        session_patch, email_patch, review_patch = _year_in_review_patches(session, [_review(2025)])
        with session_patch, email_patch as send, review_patch as review:
            await send_year_in_review({}, today=self.JANUARY)

        assert review.await_args.args[1:] == (1, 2025)
        email, sent_review, units = send.await_args.args
        assert (email, sent_review.year, units) == ("diver1@example.com", 2025, "metric")
        [mark] = session.updates
        assert mark["year_in_review_sent_for"] == 2025

    @pytest.mark.asyncio
    async def test_sends_at_most_one_batch_in_candidate_order(self) -> None:
        candidates = [_candidate(user_id) for user_id in range(1, YEAR_IN_REVIEW_BATCH_SIZE + 6)]
        session = _RecordingSession(candidates)
        reviews = [_review() for _ in candidates]
        session_patch, email_patch, review_patch = _year_in_review_patches(session, reviews)
        with session_patch, email_patch as send, review_patch:
            result = await send_year_in_review({}, today=self.JANUARY)

        assert send.await_count == YEAR_IN_REVIEW_BATCH_SIZE
        assert [call.args[0] for call in send.await_args_list] == [
            f"diver{user_id}@example.com" for user_id in range(1, YEAR_IN_REVIEW_BATCH_SIZE + 1)
        ]
        assert len(session.updates) == YEAR_IN_REVIEW_BATCH_SIZE
        assert f"Sent {YEAR_IN_REVIEW_BATCH_SIZE} year-in-review email(s) for 2025" in result

    @pytest.mark.asyncio
    async def test_a_diver_with_no_dive_in_the_year_is_skipped_without_using_a_slot(self) -> None:
        candidates = [_candidate(user_id) for user_id in range(1, YEAR_IN_REVIEW_BATCH_SIZE + 2)]
        session = _RecordingSession(candidates)
        reviews = [None, *(_review() for _ in candidates[1:])]
        session_patch, email_patch, review_patch = _year_in_review_patches(session, reviews)
        with session_patch, email_patch as send, review_patch:
            await send_year_in_review({}, today=self.JANUARY)

        sent_to = [call.args[0] for call in send.await_args_list]
        assert "diver1@example.com" not in sent_to
        assert len(sent_to) == YEAR_IN_REVIEW_BATCH_SIZE
        assert sent_to[-1] == f"diver{YEAR_IN_REVIEW_BATCH_SIZE + 1}@example.com"

    @pytest.mark.asyncio
    async def test_a_refused_send_leaves_that_diver_unmarked(self) -> None:
        """The relay's daily ceiling raises; the diver is picked up by the next run."""
        session = _RecordingSession([_candidate(1), _candidate(2)])
        session_patch, email_patch, review_patch = _year_in_review_patches(session, [_review(), _review()])
        with session_patch, email_patch as send, review_patch:
            send.side_effect = [None, RuntimeError("daily quota exceeded")]
            with pytest.raises(RuntimeError):
                await send_year_in_review({}, today=self.JANUARY)

        assert len(session.updates) == 1

    @pytest.mark.asyncio
    async def test_the_query_asks_for_the_opted_in_living_and_unsent_oldest_first(self) -> None:
        session = _RecordingSession([])
        session_patch, email_patch, review_patch = _year_in_review_patches(session, [])
        with session_patch, email_patch, review_patch:
            await send_year_in_review({}, today=self.JANUARY)

        [statement] = session.calls
        assert '"user".is_deleted IS false' in statement
        assert '"user".year_in_review_emails IS true' in statement
        assert '"user".year_in_review_sent_for IS NULL OR "user".year_in_review_sent_for <' in statement
        assert "dive.is_deleted IS false" in statement
        assert 'ORDER BY "user".created_at, "user".id' in statement


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestSendRenewalRemindersAgainstPostgres:
    """The job against a real database, for the reason `TestSendGearServiceDigestsAgainstPostgres`
    exists: the cards' mark is an ORM bulk UPDATE by primary key, and a mocked session files
    away a statement Postgres would refuse.

    Unscoped, as the cron runs it. `today` is pinned decades back so the window reaches no
    other test's rows, and assertions only ask after the rows seeded here.
    """

    TODAY = date(1990, 6, 1)

    @pytest_asyncio.fixture(autouse=True)
    async def _dispose_the_app_engine(self) -> AsyncGenerator[None]:
        await async_engine.dispose()
        yield
        await async_engine.dispose()

    def _due(self, db: Session, diver: User) -> Certification:
        card = create_certification(db, diver)
        card.expires_on = self.TODAY + timedelta(days=30)
        diver.insurance_provider = "DAN Europe"
        diver.insurance_expires_on = self.TODAY - timedelta(days=1)
        db.commit()
        return card

    @pytest.mark.asyncio
    async def test_the_marks_reach_postgres(self, db: Session, diver: User) -> None:
        card = self._due(db, diver)

        with patch("src.app.core.worker.functions.send_renewal_reminder_email", new_callable=AsyncMock) as send:
            await send_renewal_reminders({}, today=self.TODAY)

        [lines] = [call.args[1] for call in send.await_args_list if call.args[0] == diver.email]
        assert len(lines) == 2
        db.refresh(card)
        db.refresh(diver)
        assert (card.expiry_notified_stage, card.expiry_notified_for) == ("expiring_soon", card.expires_on)
        assert (diver.insurance_notified_stage, diver.insurance_notified_for) == ("expired", diver.insurance_expires_on)

    @pytest.mark.asyncio
    async def test_a_second_run_sends_nothing_new(self, db: Session, diver: User) -> None:
        self._due(db, diver)

        with patch("src.app.core.worker.functions.send_renewal_reminder_email", new_callable=AsyncMock):
            await send_renewal_reminders({}, today=self.TODAY)
        with patch("src.app.core.worker.functions.send_renewal_reminder_email", new_callable=AsyncMock) as send:
            await send_renewal_reminders({}, today=self.TODAY)

        assert diver.email not in {call.args[0] for call in send.await_args_list}

    @pytest.mark.asyncio
    async def test_an_opted_out_diver_and_a_deleted_card_are_left_alone(
        self, db: Session, diver: User, other_diver: User
    ) -> None:
        self._due(db, diver)
        diver.renewal_reminder_emails = False
        hidden = create_certification(db, other_diver)
        hidden.expires_on = self.TODAY
        hidden.is_deleted = True
        db.commit()

        with patch("src.app.core.worker.functions.send_renewal_reminder_email", new_callable=AsyncMock) as send:
            await send_renewal_reminders({}, today=self.TODAY)

        assert {diver.email, other_diver.email}.isdisjoint(call.args[0] for call in send.await_args_list)


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestSendYearInReviewAgainstPostgres:
    """The job and its queries against a real database: the mark, and the figures read back
    through the dives' own local days.

    Unscoped, as the cron runs it, over a year decades back so no other test's dives are in
    it; a diver this class seeded is marked by its own run and not eligible again.
    """

    YEAR = 1990
    JANUARY = date(1991, 1, 10)

    @pytest_asyncio.fixture(autouse=True)
    async def _dispose_the_app_engine(self) -> AsyncGenerator[None]:
        await async_engine.dispose()
        yield
        await async_engine.dispose()

    @staticmethod
    def _dive(db: Session, diver: User, start_time: datetime, **columns: Any) -> Dive:
        values: dict[str, Any] = {"duration": 2400, "max_depth": 18.0, **columns}
        dive = Dive(user_id=diver.id, dive_number=1, start_time=start_time, notes="", **values)
        db.add(dive)
        db.commit()
        return dive

    async def _run(self) -> AsyncMock:
        with patch("src.app.core.worker.functions.send_year_in_review_email", new_callable=AsyncMock) as send:
            await send_year_in_review({}, today=self.JANUARY)
        return send

    @pytest.mark.asyncio
    async def test_the_review_and_its_mark(self, db: Session, diver: User) -> None:
        # 00:30 on 1 January in Bangkok, stored in the year before: a dive of this year.
        self._dive(db, diver, datetime(self.YEAR - 1, 12, 31, 17, 30, tzinfo=UTC), utc_offset_minutes=420)
        deepest = self._dive(db, diver, datetime(self.YEAR, 6, 1, 9, 0, tzinfo=UTC), max_depth=31.0, duration=3000)
        # 03:00 on 1 January of the next year in Bangkok, stored in this one: not this year's.
        self._dive(db, diver, datetime(self.YEAR, 12, 31, 20, 0, tzinfo=UTC), utc_offset_minutes=420, max_depth=40.0)
        self._dive(db, diver, datetime(self.YEAR, 7, 1, 9, 0, tzinfo=UTC), max_depth=50.0, is_deleted=True)

        send = await self._run()

        [review] = [call.args[1] for call in send.await_args_list if call.args[0] == diver.email]
        assert review.year == self.YEAR
        assert review.dives == 2
        assert review.seconds_underwater == 5400
        assert review.deepest.uuid == deepest.uuid
        db.refresh(diver)
        assert diver.year_in_review_sent_for == self.YEAR

    @pytest.mark.asyncio
    async def test_a_second_run_does_not_send_the_year_again(self, db: Session, diver: User) -> None:
        self._dive(db, diver, datetime(self.YEAR, 6, 1, 9, 0, tzinfo=UTC))

        await self._run()
        send = await self._run()

        assert diver.email not in {call.args[0] for call in send.await_args_list}

    @pytest.mark.asyncio
    async def test_sites_and_first_sightings(self, db: Session, diver: User) -> None:
        earlier = self._dive(db, diver, datetime(self.YEAR - 2, 6, 1, 9, 0, tzinfo=UTC))
        this_year = self._dive(db, diver, datetime(self.YEAR, 6, 1, 9, 0, tzinfo=UTC))
        turtle, moray = create_species(db), create_species(db)
        site = create_dive_site(db, diver)
        db.add_all(
            [
                DiveSpecies(dive_id=earlier.id, species_id=moray.id),
                DiveSpecies(dive_id=this_year.id, species_id=moray.id),
                DiveSpecies(dive_id=this_year.id, species_id=turtle.id),
                DiveDiveSite(dive_id=this_year.id, dive_site_id=site.id),
            ]
        )
        db.commit()

        send = await self._run()

        [review] = [call.args[1] for call in send.await_args_list if call.args[0] == diver.email]
        assert (review.dive_sites, review.species, review.first_species) == (1, 2, 1)
        assert review.deepest.site_name == site.name

    @pytest.mark.asyncio
    async def test_a_dive_in_the_window_but_not_the_year_is_not_a_review(self, db: Session, diver: User) -> None:
        """Stored on the year's last day, dived on the next year's first: the pre-filter lets
        the diver through and the local day turns them away, unmarked."""
        self._dive(db, diver, datetime(self.YEAR, 12, 31, 20, 0, tzinfo=UTC), utc_offset_minutes=420)

        send = await self._run()

        assert diver.email not in {call.args[0] for call in send.await_args_list}
        db.refresh(diver)
        assert diver.year_in_review_sent_for is None

    @pytest.mark.asyncio
    async def test_the_opted_out_get_nothing(self, db: Session, diver: User) -> None:
        self._dive(db, diver, datetime(self.YEAR, 6, 1, 9, 0, tzinfo=UTC))
        diver.year_in_review_emails = False
        db.commit()

        send = await self._run()

        assert diver.email not in {call.args[0] for call in send.await_args_list}
        db.refresh(diver)
        assert diver.year_in_review_sent_for is None


class TestWorkerStartup:
    """The worker probes the blob store before any cron runs.

    It is the second writer - `purge_deleted_accounts` deletes every blob a purged diver
    owned - and unlike the API it serves no request that would surface a broken store. A
    worker that started anyway would report a successful GDPR erasure every hour while
    leaving the diver's c-card scans in the bucket.
    """

    @pytest.mark.asyncio
    async def test_it_probes_the_store_before_reporting_started(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = select_s3_backend(monkeypatch)

        await startup(cast(Any, SimpleNamespace()))

        assert client.operations() == ["put_object", "delete_object"]

    @pytest.mark.asyncio
    async def test_a_store_it_cannot_write_to_takes_the_worker_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Raising rather than warning: arq surfaces a failed startup instead of running the
        crons anyway, so the container restarts into the same loud error rather than quietly
        doing half its job."""
        client = select_s3_backend(monkeypatch)
        client.explode_on["put_object"] = ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")

        with pytest.raises(RuntimeError, match="S3_BUCKET"):
            await startup(cast(Any, SimpleNamespace()))


class TestTheMailCrons:
    """The three scheduled emails share the digest's hour, and none runs at startup: a worker
    restart must never send a round of email, and `YEAR_IN_REVIEW_BATCH_SIZE` leaves room in the
    relay's daily cap for the other two sending beside it."""

    @pytest.mark.parametrize("job", ["send_gear_service_digests", "send_renewal_reminders", "send_year_in_review"])
    def test_it_runs_daily_at_the_digest_hour_and_never_at_startup(self, job: str) -> None:
        [entry] = [cron_job for cron_job in WorkerSettings.cron_jobs if cron_job.name == f"cron:{job}"]

        assert (entry.hour, entry.minute, entry.run_at_startup) == (settings.GEAR_SERVICE_DIGEST_HOUR, 0, False)
