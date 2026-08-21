"""`DELETE /user` and the purge job that finishes what it starts.

Two halves of one feature, tested in one module because the endpoint's only lasting effect
is the state the job later acts on: the endpoint flags the row and names a date, and
`purge_deleted_accounts` is what makes that date mean anything. See
`plans/account-deletion.md` §5 and §6.

The Postgres-backed classes are skipped silently without a reachable database - on a
developer's machine that means `POSTGRES_SERVER=localhost`, since `src/.env` points at the
compose hostname. CI sets it and fails the job if anything skips. See CONTRIBUTING.md.
"""

import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
import pytest_asyncio
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1.users import erase_user
from src.app.core.config import settings
from src.app.core.db.database import async_engine
from src.app.core.worker.functions import ACCOUNT_PURGE_BATCH_SIZE, purge_deleted_accounts
from src.app.models.authentication_request import AuthenticationRequest
from src.app.models.certification import Certification
from src.app.models.certification_file import CertificationFile
from src.app.models.dive_file import DiveFile
from src.app.models.user import User
from src.app.services import blob_store
from tests.conftest import db_available
from tests.helpers.generators import create_dive, create_user


class _EraseSession(AsyncMock):
    """A session for `erase_user`: answers the conditional UPDATE with a `deleted_at` (or
    `None`, meaning the row was already pending) and the follow-up SELECT with `existing`.
    """

    def __init__(self, *, returned: datetime | None, existing: datetime | None = None) -> None:
        super().__init__()
        self._results = [returned, existing]
        self.statements: list[Any] = []
        self.committed = False

    async def execute(self, statement, parameters=None):
        self.statements.append(statement)
        result = MagicMock()
        result.scalar_one_or_none.return_value = self._results.pop(0) if self._results else None
        return result

    async def commit(self) -> None:
        self.committed = True


def _sql(statement: Any) -> str:
    return str(statement).replace("\n", " ")


class TestEraseUser:
    """The request half. Always the caller's own account - there is no other to target."""

    @staticmethod
    def _call(session: _EraseSession, current_user: dict, *, refresh_token: str | None, response: Any = None):
        return erase_user(
            request=Mock(),
            response=response or Mock(),
            current_user=current_user,
            db=session,
            access_token="mock_access_token",
            refresh_token=refresh_token,
        )

    @pytest.mark.asyncio
    async def test_flags_the_row_only_while_it_is_live(self, current_user_dict) -> None:
        """The `WHERE is_deleted = false` is the endpoint's real guard against a double
        submit: two concurrent calls both clear `get_current_user` before either commits, so
        anything short of a database predicate rewrites the deletion clock and mails twice.
        """
        session = _EraseSession(returned=datetime.now(UTC))

        with patch("src.app.api.v1.users.send_account_deletion_email", new_callable=AsyncMock):
            with patch("src.app.api.v1.users.blacklist_token", new_callable=AsyncMock):
                await self._call(session, current_user_dict, refresh_token=None)

        update = _sql(session.statements[0])
        assert update.upper().startswith('UPDATE "USER"')
        assert "SET is_deleted=" in update and "deleted_at=" in update
        assert '"user".is_deleted IS false' in update
        assert "RETURNING" in update.upper()
        assert session.committed

    @pytest.mark.asyncio
    async def test_blacklists_both_tokens_and_clears_the_cookie(self, current_user_dict) -> None:
        session = _EraseSession(returned=datetime.now(UTC))
        response = Mock()

        with patch("src.app.api.v1.users.send_account_deletion_email", new_callable=AsyncMock):
            with patch("src.app.api.v1.users.blacklist_tokens", new_callable=AsyncMock) as blacklist:
                await self._call(session, current_user_dict, refresh_token="mock_refresh_token", response=response)

        blacklist.assert_called_once_with(
            access_token="mock_access_token", refresh_token="mock_refresh_token", db=session
        )
        response.delete_cookie.assert_called_once_with(key="refresh_token")

    @pytest.mark.asyncio
    async def test_blacklists_the_access_token_alone_without_a_refresh_cookie(self, current_user_dict) -> None:
        session = _EraseSession(returned=datetime.now(UTC))

        with patch("src.app.api.v1.users.send_account_deletion_email", new_callable=AsyncMock):
            with patch("src.app.api.v1.users.blacklist_token", new_callable=AsyncMock) as blacklist:
                await self._call(session, current_user_dict, refresh_token=None)

        blacklist.assert_called_once_with(token="mock_access_token", db=session)

    @pytest.mark.asyncio
    async def test_returns_the_purge_date_and_sends_one_email(self, current_user_dict) -> None:
        deleted_at = datetime.now(UTC)
        session = _EraseSession(returned=deleted_at)

        with patch("src.app.api.v1.users.send_account_deletion_email", new_callable=AsyncMock) as send:
            with patch("src.app.api.v1.users.blacklist_token", new_callable=AsyncMock):
                result = await self._call(session, current_user_dict, refresh_token=None)

        expected = deleted_at + timedelta(days=settings.ACCOUNT_DELETION_GRACE_DAYS)
        assert result.purge_after == expected
        send.assert_called_once_with(current_user_dict["email"], expected)

    @pytest.mark.asyncio
    async def test_a_second_call_repeats_the_date_and_mails_nothing(self, current_user_dict) -> None:
        """Zero rows updated is "already pending", not an error - and the date it answers
        with is the one the *first* request set, so a retry and a double submit agree."""
        first_requested = datetime.now(UTC) - timedelta(days=3)
        session = _EraseSession(returned=None, existing=first_requested)

        with patch("src.app.api.v1.users.send_account_deletion_email", new_callable=AsyncMock) as send:
            with patch("src.app.api.v1.users.blacklist_token", new_callable=AsyncMock):
                result = await self._call(session, current_user_dict, refresh_token=None)

        assert result.purge_after == first_requested + timedelta(days=settings.ACCOUNT_DELETION_GRACE_DAYS)
        send.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_dead_relay_still_returns_the_date(self, current_user_dict) -> None:
        """The deletion has already committed by the time the email is attempted. Raising
        here would leave the user locked out *and* never told the date, which is strictly
        worse than no email - so the failure is logged and the body stands."""
        session = _EraseSession(returned=datetime.now(UTC))

        with patch(
            "src.app.api.v1.users.send_account_deletion_email",
            new_callable=AsyncMock,
            side_effect=RuntimeError("relay refused"),
        ):
            with patch("src.app.api.v1.users.blacklist_token", new_callable=AsyncMock):
                result = await self._call(session, current_user_dict, refresh_token=None)

        assert result.message == "User deleted"
        assert result.purge_after is not None

    @pytest.mark.asyncio
    async def test_is_rate_limited_per_user(self, current_user_dict) -> None:
        """It sends mail. This does not solve the double submit above - a two-request race
        beats any counter - it stops one account being used to pump the relay."""
        session = _EraseSession(returned=datetime.now(UTC))

        with patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock) as limit:
            with patch("src.app.api.v1.users.send_account_deletion_email", new_callable=AsyncMock):
                with patch("src.app.api.v1.users.blacklist_token", new_callable=AsyncMock):
                    await self._call(session, current_user_dict, refresh_token=None)

        limit.assert_awaited_once_with(
            f"account-deletion:user:{current_user_dict['id']}",
            settings.ACCOUNT_DELETION_RATE_LIMIT_PER_USER,
            settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
        )

    @pytest.mark.asyncio
    async def test_sweeps_the_users_cache_keys(self, current_user_dict) -> None:
        session = _EraseSession(returned=datetime.now(UTC))

        with patch("src.app.api.v1.users.delete_keys_by_pattern", new_callable=AsyncMock) as sweep:
            with patch("src.app.api.v1.users.send_account_deletion_email", new_callable=AsyncMock):
                with patch("src.app.api.v1.users.blacklist_token", new_callable=AsyncMock):
                    await self._call(session, current_user_dict, refresh_token=None)

        sweep.assert_awaited_once_with(f"user_{current_user_dict['id']}_*")


class _FakeSessionContext:
    """Minimal async context manager mimicking `local_session()`."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def __aenter__(self) -> Any:
        return self._db

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _PurgeSession(AsyncMock):
    """`local_session()` stand-in for the purge, dispatching on the statement.

    The job runs six shapes of statement and a mocked session has to tell them apart to be
    worth anything: the stranded-row count, the batch select, the two blob-key selects, and
    the two deletes. `user_delete_rowcount` is the knob the race test turns.
    """

    def __init__(self, *, due: list[Any], stranded: int = 0, user_delete_rowcount: int = 1) -> None:
        super().__init__()
        self._due = due
        self._stranded = stranded
        self._user_delete_rowcount = user_delete_rowcount
        self.statements: list[str] = []
        self.objects: list[Any] = []
        self.committed = 0
        self.rolled_back = 0

    async def execute(self, statement, parameters=None):
        compiled = _sql(statement)
        self.statements.append(compiled)
        self.objects.append(statement)
        result = MagicMock()

        if compiled.startswith("SELECT count"):
            result.scalar_one.return_value = self._stranded
        elif compiled.startswith('SELECT "user".id'):
            result.all.return_value = self._due
        elif "storage_key" in compiled:
            result.scalars.return_value.all.return_value = []
        elif compiled.startswith("DELETE FROM authentication_request"):
            result.rowcount = 0
        elif compiled.startswith('DELETE FROM "user"'):
            result.rowcount = self._user_delete_rowcount
        return result

    async def commit(self) -> None:
        self.committed += 1

    async def rollback(self) -> None:
        self.rolled_back += 1


def _due_row(user_id: int = 7, *, days_ago: int = 30) -> Any:
    row = MagicMock()
    row.id = user_id
    row.email = "diver@example.com"
    row.deleted_at = datetime.now(UTC) - timedelta(days=days_ago)
    return row


class TestPurgeDeletedAccounts:
    """The job's decisions, with the database mocked - what it selects, what it refuses to
    delete, and what it says about a state nothing should be able to produce."""

    @staticmethod
    def _run(session: _PurgeSession):
        return patch("src.app.core.worker.functions.local_session", return_value=_FakeSessionContext(session))

    @pytest.mark.asyncio
    async def test_an_empty_sweep_says_so(self) -> None:
        """The common case on an hourly cron."""
        session = _PurgeSession(due=[])

        with self._run(session):
            result = await purge_deleted_accounts({})

        assert "No accounts to purge" in result
        assert session.committed == 0

    @pytest.mark.asyncio
    async def test_the_cutoff_trails_now_by_the_grace_period(self) -> None:
        """UTC-aware, for the reason `purge_expired_tokens` spells out: `deleted_at` is a
        `DateTime(timezone=True)`, so a naive `datetime.now()` compares against it off by
        the host's UTC offset."""
        session = _PurgeSession(due=[])
        before = datetime.now(UTC)
        grace = timedelta(days=settings.ACCOUNT_DELETION_GRACE_DAYS)

        with self._run(session):
            await purge_deleted_accounts({})

        batch_select = next(o for o in session.objects if _sql(o).startswith('SELECT "user".id'))
        cutoff = next(v for v in batch_select.compile().params.values() if isinstance(v, datetime))
        assert cutoff.tzinfo is not None, "a naive cutoff compares off by the host's UTC offset"
        assert before - grace - timedelta(minutes=1) <= cutoff <= datetime.now(UTC) - grace

    @pytest.mark.asyncio
    async def test_a_restored_account_is_not_deleted(self) -> None:
        """The finding that mattered most in review. The batch is selected once and deleted
        one account at a time, so `POST /auth/restore` can commit in the window between -
        and a `DELETE` naming the id alone would then destroy a live account. Zero rows is
        the normal outcome, not an error.
        """
        session = _PurgeSession(due=[_due_row()], user_delete_rowcount=0)

        with self._run(session):
            with patch("src.app.core.worker.functions.blob_store") as store:
                result = await purge_deleted_accounts({})

        assert session.committed == 0
        assert session.rolled_back == 1
        store.delete_after_commit.assert_not_called()
        assert "Purged 0" in result

    @pytest.mark.asyncio
    async def test_the_guarded_delete_repeats_the_selections_predicate(self) -> None:
        session = _PurgeSession(due=[_due_row()])

        with self._run(session):
            with patch("src.app.core.worker.functions.blob_store"):
                await purge_deleted_accounts({})

        (user_delete,) = [s for s in session.statements if s.startswith('DELETE FROM "user"')]
        assert '"user".is_deleted IS true' in user_delete
        assert '"user".deleted_at IS NOT NULL' in user_delete
        assert '"user".deleted_at <' in user_delete

    @pytest.mark.asyncio
    async def test_the_sign_in_rows_go_by_email_because_no_cascade_reaches_them(self) -> None:
        """`purpose="sign_in"` rows carry a `NULL user_id` by design, so the account's
        `ON DELETE CASCADE` cannot follow them."""
        session = _PurgeSession(due=[_due_row()])

        with self._run(session):
            with patch("src.app.core.worker.functions.blob_store"):
                await purge_deleted_accounts({})

        (auth_delete,) = [s for s in session.statements if s.startswith("DELETE FROM authentication_request")]
        assert "authentication_request.email =" in auth_delete

    @pytest.mark.asyncio
    async def test_a_flagged_row_with_no_clock_is_warned_about_not_skipped_quietly(self, caplog) -> None:
        """`deleted_at < :cutoff` is NULL for such a row, so it is dark forever and the
        selection cannot see it. This warning is what would catch a later feature borrowing
        `is_deleted` for something that is not a deletion request."""
        session = _PurgeSession(due=[], stranded=2)

        with caplog.at_level(logging.WARNING):
            with self._run(session):
                await purge_deleted_accounts({})

        assert any("never be purged" in record.getMessage() for record in caplog.records)

    @pytest.mark.asyncio
    async def test_one_failure_does_not_take_the_batch_with_it(self) -> None:
        """One transaction per account, so a poisoned row strands itself."""
        session = _PurgeSession(due=[_due_row(1), _due_row(2)])

        with self._run(session):
            with patch("src.app.core.worker.functions.blob_store") as store:
                store.delete_after_commit.side_effect = [RuntimeError("boom"), None]
                result = await purge_deleted_accounts({})

        assert "Purged 1" in result

    @pytest.mark.asyncio
    async def test_a_full_batch_says_more_may_be_due(self, caplog) -> None:
        """A silent cap reads as "purged everything" when it did not."""
        session = _PurgeSession(due=[_due_row(i) for i in range(ACCOUNT_PURGE_BATCH_SIZE)])

        with caplog.at_level(logging.INFO):
            with self._run(session):
                with patch("src.app.core.worker.functions.blob_store"):
                    await purge_deleted_accounts({})

        assert any("more accounts may be due" in record.getMessage() for record in caplog.records)


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestPurgeDeletedAccountsAgainstPostgres:
    """The job against a real database, because what it has to get right is a `WHERE`
    clause, a cascade and a filesystem - and a mocked session evaluates none of the three.

    Unscoped, as the cron runs it: it purges every eligible account in the database, not
    only this test's, so the assertions only ever ask after rows the test seeded.
    """

    @pytest_asyncio.fixture(autouse=True)
    async def _dispose_the_app_engine(self) -> AsyncGenerator[None]:
        """The job opens its own session from the module-level `local_session`, bound to the
        app's session-lifetime engine, and a pooled asyncpg connection belongs to the loop
        that opened it."""
        await async_engine.dispose()
        yield
        await async_engine.dispose()

    @staticmethod
    def _request_deletion(db: Session, user: User, *, days_ago: float) -> None:
        user.is_deleted = True
        user.deleted_at = datetime.now(UTC) - timedelta(days=days_ago)
        db.commit()

    @pytest.mark.asyncio
    async def test_a_past_deadline_account_is_destroyed_and_a_same_day_request_is_not(self, db: Session) -> None:
        overdue = create_user(db)
        fresh = create_user(db)
        create_dive(db, overdue)
        self._request_deletion(db, overdue, days_ago=settings.ACCOUNT_DELETION_GRACE_DAYS + 1)
        self._request_deletion(db, fresh, days_ago=0)
        overdue_id, fresh_id = overdue.id, fresh.id
        db.expunge_all()

        await purge_deleted_accounts({})

        assert db.get(User, overdue_id) is None
        assert db.get(User, fresh_id) is not None

    @pytest.mark.asyncio
    async def test_a_live_account_is_never_touched(self, db: Session) -> None:
        living = create_user(db)
        living_id = living.id
        db.expunge_all()

        await purge_deleted_accounts({})

        assert db.get(User, living_id) is not None

    @pytest.mark.asyncio
    async def test_the_accounts_sign_in_requests_go_with_it(self, db: Session) -> None:
        """They carry a `NULL user_id`, so nothing else would ever remove them within the
        purge's reach."""
        diver = create_user(db)
        row = AuthenticationRequest(
            email=diver.email,
            token_hash=uuid7().hex,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            purpose="sign_in",
        )
        db.add(row)
        db.commit()
        row_id = row.id
        self._request_deletion(db, diver, days_ago=settings.ACCOUNT_DELETION_GRACE_DAYS + 1)
        db.expunge_all()

        await purge_deleted_accounts({})

        assert db.get(AuthenticationRequest, row_id) is None

    @pytest.mark.asyncio
    async def test_the_stored_files_are_unlinked_not_just_their_rows(self, db: Session) -> None:
        """The half the cascade cannot do. `DELETE FROM "user"` retires `dive_file` and
        `certification_file` inside Postgres, so SQLAlchemy never sees those rows and
        `delete_after_commit` is never called by anything but the job itself - a test that
        counts rows passes while every c-card scan stays on the volume.

        Has to be a real-database test for a second reason: `delete_after_commit` fires on
        `Session.after_commit`, and the `AsyncMock` sessions much of the suite uses never
        commit.
        """
        diver = create_user(db)
        dive = create_dive(db, diver)
        certification = Certification(user_id=diver.id, agency="padi", name="Rescue Diver", notes="")
        db.add(certification)
        db.flush()

        dive_key = blob_store.new_key("dive-files", sha256="c" * 64)
        card_key = blob_store.new_key("certification-files", sha256="d" * 64)
        await blob_store.put(dive_key, b"dive computer export")
        await blob_store.put(card_key, b"card scan")

        db.add_all(
            [
                DiveFile(
                    user_id=diver.id,
                    dive_id=dive.id,
                    sha256="c" * 64,
                    content_type="application/octet-stream",
                    byte_size=20,
                    original_filename="dive.uddf",
                    parser_key="uddf",
                    storage_key=dive_key,
                ),
                CertificationFile(
                    certification_id=certification.id,
                    side="front",
                    content_type="image/jpeg",
                    byte_size=9,
                    original_filename="card.jpg",
                    sha256="d" * 64,
                    storage_key=card_key,
                ),
            ]
        )
        db.commit()
        assert blob_store.exists(dive_key) and blob_store.exists(card_key)

        self._request_deletion(db, diver, days_ago=settings.ACCOUNT_DELETION_GRACE_DAYS + 1)
        diver_id = diver.id
        db.expunge_all()

        await purge_deleted_accounts({})

        assert db.get(User, diver_id) is None
        assert not blob_store.exists(dive_key), "the dive-computer export outlived the account"
        assert not blob_store.exists(card_key), "the c-card scan outlived the account"
