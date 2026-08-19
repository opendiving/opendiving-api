"""Unit tests for the Arq worker background tasks."""

from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.core.db.database import async_engine
from src.app.core.worker.functions import _due_text, purge_expired_tokens, send_gear_service_digests
from src.app.models.user import User
from src.app.schemas.gear_service import ServiceKind, ServiceStatus
from tests.conftest import db_available
from tests.helpers.generators import create_gear_item, create_gear_service_schedule


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
        """Return the app's own pool to a clean state after each test in this class.

        These tests can't take the `async_db` fixture and its per-test engine, the way
        most of the database-backed suite does: the job under test opens its own session
        from the module-level `local_session`, which is bound to the app's
        session-lifetime `async_engine`. pytest-asyncio gives each test a fresh event
        loop, and a pooled asyncpg connection belongs to the loop that opened it, so
        without this the second test here is handed the first test's dead-loop connection
        and dies with "attached to a different loop". Teardown runs inside the test's own
        loop, which is what makes the close legal.

        `test_export_loader.py`'s `_load` disposes the same engine for the same reason,
        in a `try/finally` around its own helper. A fixture rather than a helper here
        because the job is called directly, with no wrapper of ours to put the `finally`
        in.
        """
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
