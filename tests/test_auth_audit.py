"""The auth audit trail: which sites write a row, which deliberately do not, what a row may
never contain, and how long each tier of them lives.

**The exclusions carry as much weight as the events here**, and are tested as explicitly.
Every one of them sits on a path that satisfies one of the write-based criteria - a
credential row committed, a token minted - and is excluded on the standing rule that
separates "a recurring, expected event" from "rare and means something". A test that only
checked the positive cases would pass just as well against a version that logged every
refresh rotation, which is the version this design exists to not be.

The retention sweep and the account purge's second arm run against real Postgres, because
both are `WHERE` clauses and a mocked session evaluates neither. Those classes skip
silently without a reachable database - `POSTGRES_SERVER=localhost` on a developer's
machine. See CONTRIBUTING.md.
"""

import uuid as uuid_pkg
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1.auth import _REFRESH_REPLAY_AUDIT_THRESHOLD, _warn_if_revoked
from src.app.core.db.database import async_engine
from src.app.core.utils.request_context import RequestContext
from src.app.core.worker.functions import (
    AUTH_AUDIT_ANONYMOUS_RETENTION,
    AUTH_AUDIT_RETENTION,
    AUTHENTICATION_REQUEST_RETENTION,
    purge_expired_auth_audit_events,
    purge_expired_user_sessions,
)
from src.app.crud.crud_auth_audit_events import record_auth_event
from src.app.models.auth_audit_event import AuthAuditEvent
from src.app.models.user import User
from src.app.models.user_session import UserSession
from src.app.schemas.auth_audit_event import AuthEventType
from tests.conftest import db_available, unique_email
from tests.helpers.mocks import awaited_kwargs

CONTEXT = RequestContext(ip="203.0.113.7", user_agent="Mozilla/5.0 (X11; Linux x86_64) TestAgent/1.0")

needs_a_database = pytest.mark.skipif(not db_available(), reason="No database connection available")


def _request(cookies: dict[str, str] | None = None) -> Mock:
    request = Mock()
    request.client = Mock(host=CONTEXT.ip)
    request.headers = {"user-agent": CONTEXT.user_agent}
    request.cookies = cookies or {}
    return request


class TestTheVocabularyIsClosed:
    def test_no_event_name_exceeds_the_column(self) -> None:
        """The column is a plain `VARCHAR` with no DB-level `CHECK`, so the enum is the
        whole enforcement - which makes the width the one thing the database still has an
        opinion about, and an over-long member a 500 at the moment of writing rather than a
        validation error anyone would see coming."""
        from src.app.models.auth_audit_event import EVENT_TYPE_MAX_LENGTH

        assert max(len(event.value) for event in AuthEventType) <= EVENT_TYPE_MAX_LENGTH

    def test_every_member_is_snake_case_and_distinct(self) -> None:
        values = [event.value for event in AuthEventType]

        assert len(values) == len(set(values))
        assert all(value.islower() and " " not in value for value in values)


class TestNothingTokenDerivedIsEverRecorded:
    """OWASP's never-log list, as a property of the writer rather than of each call site.

    The fact of the artifact, never the artifact: no token, no `jti`, no token hash, no
    sign-in code and no code digest. The enforcement is that `record_auth_event` has nowhere
    to put one - there is no free-text column - and this is what fails if a column that
    could hold one is ever added.
    """

    def test_the_table_has_no_column_that_could_hold_a_secret(self) -> None:
        columns = set(AuthAuditEvent.__table__.columns.keys())

        assert columns == {"id", "event_type", "ip", "user_agent", "user_id", "email", "provider", "created_at"}

    def test_the_write_schema_accepts_nothing_else(self) -> None:
        """`extra="forbid"`, so a future call site cannot smuggle a field past the model
        even if the table later grows one."""
        from pydantic import ValidationError

        from src.app.schemas.auth_audit_event import AuthAuditEventCreateInternal

        with pytest.raises(ValidationError):
            AuthAuditEventCreateInternal(
                event_type=AuthEventType.LOGOUT,
                ip="1.2.3.4",
                user_agent="x",
                token="a.b.c",  # type: ignore[call-arg]
            )


class TestTheRequestContextIsBounded:
    """An over-length value must never turn an auth request into a 500 on a
    `StringDataRightTruncation` - which is exactly what the "failures propagate, nothing is
    swallowed" rule would otherwise cost on the one path that must not fail this way.
    """

    def test_a_forged_user_agent_is_truncated_to_the_column(self) -> None:
        from src.app.core.utils.request_context import MAX_USER_AGENT_LENGTH

        request = Mock()
        request.client = Mock(host="1.2.3.4")
        request.headers = {"user-agent": "A" * 10_000}

        assert len(RequestContext.from_request(request).user_agent) == MAX_USER_AGENT_LENGTH

    def test_a_forged_forwarded_address_is_truncated_too(self) -> None:
        """`client_ip` returns the right-most `X-Forwarded-For` element that is not a
        trusted proxy, and nothing validates that element as an address - so behind a
        configured proxy it is an arbitrary caller-written string."""
        from src.app.core.utils.request_context import MAX_IP_LENGTH

        request = Mock()
        request.client = Mock(host="B" * 10_000)
        request.headers = {}

        assert len(RequestContext.from_request(request).ip) == MAX_IP_LENGTH

    def test_an_absent_user_agent_is_empty_rather_than_invented(self) -> None:
        """ "This client sent no User-Agent" is a fact worth keeping distinct from any label
        we could make up for it."""
        request = Mock()
        request.client = Mock(host="1.2.3.4")
        request.headers = {}

        assert RequestContext.from_request(request).user_agent == ""


class TestWhichSitesWriteAnEvent:
    """One case per floor event that can be driven without a database, asserted through
    `record_auth_event` at the site rather than by counting rows."""

    @staticmethod
    def _events(recorder: AsyncMock) -> list[AuthEventType]:
        return [call.kwargs["event_type"] for call in recorder.await_args_list]

    @pytest.mark.asyncio
    async def test_the_auth_request_event_is_written_user_less(self, mock_db) -> None:
        """The structural guarantee: `request_email_link` never queries `crud_users`, so
        this event cannot carry a `user_id` without reversing it. It carries the address,
        which is what `authentication_request` already stores."""
        from src.app.api.v1.auth import request_email_link
        from src.app.schemas.auth import EmailAuthRequest

        with (
            patch("src.app.api.v1.auth.crud_authentication_requests") as requests,
            patch("src.app.api.v1.auth.send_magic_link_email", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.record_auth_event", new_callable=AsyncMock) as recorder,
        ):
            requests.count = AsyncMock(return_value=0)
            requests.create = AsyncMock(return_value=Mock(uuid=uuid7()))

            await request_email_link(_request(), EmailAuthRequest(email="Someone@Example.com"), mock_db)

        recorder.assert_awaited_once()
        written = awaited_kwargs(recorder)
        assert written["event_type"] is AuthEventType.AUTH_REQUEST_CREATED
        assert "user_id" not in written or written["user_id"] is None
        assert written["email"] == "someone@example.com"

    @pytest.mark.asyncio
    async def test_a_wrong_code_writes_one_and_commits_before_the_401(self, mock_db) -> None:
        """`async_get_db` does not commit on unwind, so a row left in flight on this path is
        silently lost - and this path always raises."""
        from src.app.api.v1.auth import verify_email_code
        from src.app.core.exceptions.http_exceptions import UnauthorizedException
        from src.app.schemas.auth import EmailCodeVerifyRequest

        row = {
            "id": 1,
            "email": "diver@example.com",
            "code_hash": "0" * 64,
            "invalidated_at": None,
            "used_at": None,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }

        with (
            patch("src.app.api.v1.auth.crud_authentication_requests") as requests,
            patch("src.app.api.v1.auth.register_failed_code_attempt", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.record_auth_event", new_callable=AsyncMock) as recorder,
        ):
            requests.get = AsyncMock(return_value=row)

            with pytest.raises(UnauthorizedException):
                await verify_email_code(
                    _request(), EmailCodeVerifyRequest(request_id=uuid7(), code="000000"), Mock(), mock_db
                )

        assert self._events(recorder) == [AuthEventType.SIGN_IN_CODE_FAILED]
        # The default, which is what commits it - and the reason the default is `True`.
        assert awaited_kwargs(recorder).get("commit", True) is True

    @pytest.mark.asyncio
    async def test_a_wrong_code_records_no_digest_of_it(self, mock_db) -> None:
        """The never-log rule at the one site most tempted to break it: the failure is
        recorded, the guess is not, and neither is the stored hash it was compared against.
        """
        from src.app.api.v1.auth import verify_email_code
        from src.app.core.exceptions.http_exceptions import UnauthorizedException
        from src.app.schemas.auth import EmailCodeVerifyRequest

        row = {
            "id": 1,
            "email": "diver@example.com",
            "code_hash": "0" * 64,
            "invalidated_at": None,
            "used_at": None,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }

        with (
            patch("src.app.api.v1.auth.crud_authentication_requests") as requests,
            patch("src.app.api.v1.auth.register_failed_code_attempt", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.record_auth_event", new_callable=AsyncMock) as recorder,
        ):
            requests.get = AsyncMock(return_value=row)

            with pytest.raises(UnauthorizedException):
                await verify_email_code(
                    _request(), EmailCodeVerifyRequest(request_id=uuid7(), code="123456"), Mock(), mock_db
                )

        recorded = str(awaited_kwargs(recorder))
        assert "123456" not in recorded
        assert "0" * 64 not in recorded

    @pytest.mark.asyncio
    async def test_signing_in_writes_exactly_one_event_naming_the_provider(self, mock_db) -> None:
        """Emitted at the funnel and nowhere else. All four providers reach it, so a second
        emission at a resolve site would double-count every sign-in in the app."""
        from src.app.api.v1.auth import _start_onboarding_or_sign_in
        from src.app.services.auth_service import AuthenticatedUser

        with (
            patch("src.app.api.v1.auth.issue_tokens", new_callable=AsyncMock) as issue,
            patch("src.app.api.v1.auth.record_auth_event", new_callable=AsyncMock) as recorder,
        ):
            issue.return_value = {"access_token": "a", "token_type": "bearer"}

            await _start_onboarding_or_sign_in(
                Mock(),
                AuthenticatedUser(user={"id": 7, "uuid": uuid7()}, provider="passkey"),
                db=mock_db,
                context=CONTEXT,
            )

        assert self._events(recorder) == [AuthEventType.SIGN_IN_SUCCEEDED]
        assert awaited_kwargs(recorder)["provider"] == "passkey"
        assert awaited_kwargs(recorder)["user_id"] == 7

    @pytest.mark.asyncio
    async def test_onboarding_writes_a_user_less_event(self, mock_db) -> None:
        """ "Registration-request creation" - there is no registration table, so the
        onboarding JWT is the registration request."""
        from src.app.api.v1.auth import _start_onboarding_or_sign_in
        from src.app.services.auth_service import OnboardingRequired

        with (
            patch("src.app.api.v1.auth.create_onboarding_token", new_callable=AsyncMock) as mint,
            patch("src.app.api.v1.auth.record_auth_event", new_callable=AsyncMock) as recorder,
        ):
            mint.return_value = "an-onboarding-token"

            await _start_onboarding_or_sign_in(
                Mock(),
                OnboardingRequired(
                    email="new@example.com", provider="google", provider_user_id="g-1", name="New", avatar=None
                ),
                db=mock_db,
                context=CONTEXT,
            )

        assert self._events(recorder) == [AuthEventType.ONBOARDING_STARTED]
        assert awaited_kwargs(recorder)["email"] == "new@example.com"
        assert awaited_kwargs(recorder).get("user_id") is None

    @pytest.mark.asyncio
    async def test_being_offered_a_restore_writes_one(self, mock_db) -> None:
        """The `DeletionPending` branch mints no session and signs nobody in, so it is
        emphatically not a sign-in - but somebody proving an identity against an account
        inside its grace period is worth a row of its own."""
        from src.app.api.v1.auth import _start_onboarding_or_sign_in
        from src.app.services.auth_service import DeletionPending

        row = {"id": 9, "uuid": uuid7(), "email": "gone@example.com", "deleted_at": datetime.now(UTC)}

        with (
            patch("src.app.api.v1.auth.create_restore_token", new_callable=AsyncMock) as mint,
            patch("src.app.api.v1.auth.record_auth_event", new_callable=AsyncMock) as recorder,
        ):
            mint.return_value = "a-restore-token"

            await _start_onboarding_or_sign_in(Mock(), DeletionPending.for_row(row), db=mock_db, context=CONTEXT)

        assert self._events(recorder) == [AuthEventType.RESTORE_OFFERED]
        assert awaited_kwargs(recorder)["user_id"] == 9


class TestTheNamedExclusions:
    """Each of these sites satisfies one of the write-based criteria and is deliberately
    silent anyway. They follow one standing rule - the *Nothing is logged* bullet of
    `DECISIONS.md` §"A refresh token is only as alive as its account", which contrasts a
    recurring expected event with one that is rare and means something.
    """

    @pytest.mark.asyncio
    async def test_renaming_a_passkey_is_not_an_auth_event(self, mock_db) -> None:
        """It commits to `webauthn_credential` and so turns up in the sweep, but a label
        change leaves the credential's power exactly where it was."""
        from src.app.api.v1.passkeys import patch_passkey
        from src.app.schemas.webauthn_credential import WebauthnCredentialUpdate

        with (
            patch("src.app.api.v1.passkeys.fetch_owned_or_raise", new_callable=AsyncMock),
            patch("src.app.api.v1.passkeys.crud_webauthn_credentials") as crud,
            patch("src.app.api.v1.passkeys.record_auth_event", new_callable=AsyncMock) as recorder,
        ):
            crud.update = AsyncMock()

            await patch_passkey(
                uuid7(), WebauthnCredentialUpdate(name="Renamed"), {"id": 7, "email": "d@example.com"}, mock_db
            )

        recorder.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_ordinary_rotation_writes_nothing(self, mock_db) -> None:
        """Once per access-token lifetime per device is the definition of recurring and
        expected, and `user_session.last_used_at` already records the liveness a row here
        would restate."""
        from src.app.api.v1.auth import refresh_access_token
        from src.app.core.schemas import TokenData

        session_uuid = uuid7()

        with (
            patch("src.app.api.v1.auth.verify_token", new_callable=AsyncMock) as verify,
            patch("src.app.api.v1.auth.crud_users") as users,
            patch("src.app.api.v1.auth.live_session_for", new_callable=AsyncMock) as session,
            patch("src.app.api.v1.auth.blacklist_token", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.issue_tokens", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.record_auth_event", new_callable=AsyncMock) as recorder,
        ):
            verify.return_value = TokenData(user_uuid=uuid_pkg.uuid4(), session_uuid=session_uuid)
            users.get = AsyncMock(return_value={"id": 7})
            session.return_value = Mock(uuid=session_uuid)

            await refresh_access_token(_request(cookies={"refresh_token": "good"}), Mock(), mock_db)

        recorder.assert_not_called()

    def test_a_cap_eviction_writes_nothing(self) -> None:
        """Housekeeping. The diver did not ask for it and cannot act on it, and one sign-in
        would otherwise be able to emit a hundred rows.

        Asserted against the source because there is no behaviour to drive: the absence of
        a write is what is being pinned, and the module that would have to contain it is
        the one that evicts.
        """
        from pathlib import Path

        from src.app.crud import crud_user_sessions

        assert "record_auth_event" not in Path(crud_user_sessions.__file__).read_text()

    @pytest.mark.asyncio
    async def test_the_provider_row_inside_account_creation_gets_no_event_of_its_own(self, mock_db) -> None:
        """Creating an account writes an `authentication_provider` row inside the same
        transaction. That is part of the act, and account-created is the act's one event - a
        second row would record it twice."""
        from src.app.api.v1.auth import complete_profile
        from src.app.core.schemas import OnboardingTokenData
        from src.app.schemas.auth import ProfileCompletionRequest

        with (
            patch("src.app.api.v1.auth.verify_onboarding_token", new_callable=AsyncMock) as verify,
            patch("src.app.api.v1.auth.crud_users") as users,
            patch("src.app.api.v1.auth.crud_authentication_providers") as providers,
            patch("src.app.api.v1.auth.blacklist_token", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.issue_tokens", new_callable=AsyncMock) as issue,
            patch("src.app.api.v1.auth.record_auth_event", new_callable=AsyncMock) as recorder,
        ):
            verify.return_value = OnboardingTokenData(
                email="new@example.com", provider="google", provider_user_id="g-1", name="New", avatar=None
            )
            users.exists = AsyncMock(return_value=False)
            users.create = AsyncMock(return_value=Mock(id=42, uuid=uuid7()))
            providers.create = AsyncMock()
            issue.return_value = {"access_token": "a", "token_type": "bearer"}

            await complete_profile(
                _request(),
                ProfileCompletionRequest(onboarding_token="good", name="New", username="newperson"),
                Mock(),
                mock_db,
            )

        events = [call.kwargs["event_type"] for call in recorder.await_args_list]
        assert events == [AuthEventType.ACCOUNT_CREATED]
        # In the account's own transaction, so a creation that fails leaves no row claiming
        # it succeeded.
        assert awaited_kwargs(recorder)["commit"] is False

    @pytest.mark.asyncio
    async def test_superseding_a_live_request_writes_nothing_of_its_own(self, mock_db) -> None:
        """`request_email_link` invalidates any previous live request before minting the
        new one. That is housekeeping of the creation, which is the event."""
        from src.app.api.v1.auth import request_email_link
        from src.app.schemas.auth import EmailAuthRequest

        with (
            patch("src.app.api.v1.auth.crud_authentication_requests") as requests,
            patch("src.app.api.v1.auth.send_magic_link_email", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.record_auth_event", new_callable=AsyncMock) as recorder,
        ):
            requests.count = AsyncMock(return_value=2)
            requests.update = AsyncMock()
            requests.create = AsyncMock(return_value=Mock(uuid=uuid7()))

            await request_email_link(_request(), EmailAuthRequest(email="someone@example.com"), mock_db)

        assert recorder.await_count == 1


class TestTheRefreshReplayThreshold:
    """The two-tab rotation race lands on the same branch a stolen cookie does, in
    milliseconds, and it is benign, documented and not especially rare. An unconditioned
    audit row would put the cry-wolf defect `token_blacklist.revoked_at` was added to fix
    straight into the audit table.

    **Only the row is conditioned.** The `WARNING` still fires either way, gap attached, so
    nothing that was visible has become invisible - what the threshold buys is that a row's
    existence *means* replay-not-race.
    """

    @pytest.mark.asyncio
    async def test_a_sub_threshold_gap_writes_no_row(self, mock_db, caplog) -> None:
        just_now = datetime.now(UTC) - timedelta(milliseconds=4)

        with (
            patch("src.app.api.v1.auth.revocation_time", new_callable=AsyncMock) as revoked,
            patch("src.app.api.v1.auth.record_auth_event", new_callable=AsyncMock) as recorder,
        ):
            revoked.return_value = just_now

            await _warn_if_revoked("a-token", mock_db, CONTEXT)

        recorder.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_warning_still_fires_for_the_race(self, mock_db, caplog) -> None:
        """The half that must not be conditioned: an operator reading logs still sees every
        presentation, which is what `revoked_at` bought in the first place."""
        import logging

        with (
            patch("src.app.api.v1.auth.revocation_time", new_callable=AsyncMock) as revoked,
            patch("src.app.api.v1.auth.record_auth_event", new_callable=AsyncMock),
        ):
            revoked.return_value = datetime.now(UTC) - timedelta(milliseconds=4)

            with caplog.at_level(logging.DEBUG, logger="src.app.api.v1.auth"):
                await _warn_if_revoked("a-token", mock_db, CONTEXT)

        assert [record.levelno for record in caplog.records] == [logging.WARNING]

    @pytest.mark.asyncio
    async def test_a_gap_past_the_threshold_writes_one(self, mock_db) -> None:
        long_ago = datetime.now(UTC) - _REFRESH_REPLAY_AUDIT_THRESHOLD - timedelta(seconds=1)

        with (
            patch("src.app.api.v1.auth.revocation_time", new_callable=AsyncMock) as revoked,
            patch("src.app.api.v1.auth.token_subject") as subject,
            patch("src.app.api.v1.auth.crud_users") as users,
            patch("src.app.api.v1.auth.record_auth_event", new_callable=AsyncMock) as recorder,
        ):
            revoked.return_value = long_ago
            subject.return_value = str(uuid7())
            users.get = AsyncMock(return_value={"id": 7})

            await _warn_if_revoked("a-token", mock_db, CONTEXT)

        recorder.assert_awaited_once()
        assert awaited_kwargs(recorder)["event_type"] is AuthEventType.REFRESH_REPLAY_DETECTED
        assert awaited_kwargs(recorder)["user_id"] == 7

    @pytest.mark.asyncio
    async def test_a_replay_for_a_purged_account_still_writes_a_user_less_row(self, mock_db) -> None:
        """The one row that satisfies neither arm of the erasure - no user to cascade from
        and no email to match - and so is bounded by the user-less retention tier alone."""
        long_ago = datetime.now(UTC) - _REFRESH_REPLAY_AUDIT_THRESHOLD - timedelta(hours=2)

        with (
            patch("src.app.api.v1.auth.revocation_time", new_callable=AsyncMock) as revoked,
            patch("src.app.api.v1.auth.token_subject") as subject,
            patch("src.app.api.v1.auth.crud_users") as users,
            patch("src.app.api.v1.auth.record_auth_event", new_callable=AsyncMock) as recorder,
        ):
            revoked.return_value = long_ago
            subject.return_value = str(uuid7())
            users.get = AsyncMock(return_value=None)

            await _warn_if_revoked("a-token", mock_db, CONTEXT)

        assert awaited_kwargs(recorder)["user_id"] is None

    @pytest.mark.asyncio
    async def test_a_token_that_was_never_revoked_is_silent(self, mock_db, caplog) -> None:
        """Garbage is noise. Logging it would let anyone fill the operator's log by posting
        cookies, and would drown the one line that means something."""
        with (
            patch("src.app.api.v1.auth.revocation_time", new_callable=AsyncMock) as revoked,
            patch("src.app.api.v1.auth.record_auth_event", new_callable=AsyncMock) as recorder,
        ):
            revoked.return_value = None

            await _warn_if_revoked("garbage", mock_db, CONTEXT)

        recorder.assert_not_called()
        assert caplog.records == []

    def test_the_threshold_is_far_above_the_race_and_far_below_a_theft(self) -> None:
        """Three orders of magnitude above the millisecond-scale race, and negligible
        against a replay that lands minutes or hours later."""
        assert timedelta(seconds=1) <= _REFRESH_REPLAY_AUDIT_THRESHOLD <= timedelta(minutes=1)


@needs_a_database
class TestRetentionSweepAgainstPostgres:
    """Two tiers in one statement, against the database that evaluates them.

    Unscoped, as the cron runs it: it deletes every eligible row in the database, not only
    this test's, so assertions only ever ask after rows the test seeded.
    """

    @pytest_asyncio.fixture(autouse=True)
    async def _dispose_the_app_engine(self) -> AsyncGenerator[None]:
        """The job opens its own session from the module-level `local_session`, bound to the
        app's session-lifetime engine, and a pooled asyncpg connection belongs to the loop
        that opened it - the same reason the other worker suites do this."""
        await async_engine.dispose()
        yield
        await async_engine.dispose()

    @staticmethod
    def _event(db: Session, *, created_at: datetime, user: User | None = None, email: str | None = None) -> int:
        row = AuthAuditEvent(
            event_type=AuthEventType.SIGN_IN_SUCCEEDED if user else AuthEventType.AUTH_REQUEST_CREATED,
            ip="203.0.113.7",
            user_agent="TestAgent/1.0",
            user_id=user.id if user else None,
            email=email,
            created_at=created_at,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        row_id = row.id
        db.expunge(row)
        return row_id

    @staticmethod
    def _still_there(db: Session, row_id: int) -> bool:
        return db.get(AuthAuditEvent, row_id) is not None

    @pytest.mark.asyncio
    async def test_an_old_account_tied_row_is_swept(self, db: Session, diver: User) -> None:
        row_id = self._event(db, created_at=datetime.now(UTC) - AUTH_AUDIT_RETENTION - timedelta(days=1), user=diver)

        await purge_expired_auth_audit_events({})

        assert not self._still_there(db, row_id)

    @pytest.mark.asyncio
    async def test_a_recent_account_tied_row_survives(self, db: Session, diver: User) -> None:
        row_id = self._event(db, created_at=datetime.now(UTC) - AUTH_AUDIT_RETENTION + timedelta(days=1), user=diver)

        await purge_expired_auth_audit_events({})

        assert self._still_there(db, row_id)

    @pytest.mark.asyncio
    async def test_a_user_less_row_goes_on_the_short_tier(self, db: Session) -> None:
        """The asymmetry that is the whole point: an address typed by somebody who never
        signed up must not survive thirteen times longer here than the
        `authentication_request` row that recorded the same act."""
        row_id = self._event(
            db,
            created_at=datetime.now(UTC) - AUTH_AUDIT_ANONYMOUS_RETENTION - timedelta(hours=1),
            email=unique_email(),
        )

        await purge_expired_auth_audit_events({})

        assert not self._still_there(db, row_id)

    @pytest.mark.asyncio
    async def test_a_user_less_row_inside_the_short_tier_survives(self, db: Session) -> None:
        row_id = self._event(
            db,
            created_at=datetime.now(UTC) - AUTH_AUDIT_ANONYMOUS_RETENTION + timedelta(hours=1),
            email=unique_email(),
        )

        await purge_expired_auth_audit_events({})

        assert self._still_there(db, row_id)

    @pytest.mark.asyncio
    async def test_a_user_less_row_older_than_seven_days_goes_while_an_account_tied_one_stays(
        self, db: Session, diver: User
    ) -> None:
        """Both tiers in one sweep, which is the case a single-tier implementation passes
        every other test in this class while getting wrong."""
        middle_aged = datetime.now(UTC) - AUTH_AUDIT_ANONYMOUS_RETENTION - timedelta(days=1)
        anonymous = self._event(db, created_at=middle_aged, email=unique_email())
        account_tied = self._event(db, created_at=middle_aged, user=diver)

        await purge_expired_auth_audit_events({})

        assert not self._still_there(db, anonymous)
        assert self._still_there(db, account_tied)

    def test_the_short_tier_is_the_authentication_request_window(self) -> None:
        """Not independently chosen: the equivalence the design leans on - that recording
        an address here puts nothing in the database `authentication_request` does not -
        holds for duration only if the two agree."""
        assert AUTH_AUDIT_ANONYMOUS_RETENTION == AUTHENTICATION_REQUEST_RETENTION


@needs_a_database
class TestSessionSweepAgainstPostgres:
    """A row that can no longer authenticate anything, gone. The predicate is the exact
    complement of the liveness one, which is why both live in `crud_user_sessions`."""

    @pytest_asyncio.fixture(autouse=True)
    async def _dispose_the_app_engine(self) -> AsyncGenerator[None]:
        await async_engine.dispose()
        yield
        await async_engine.dispose()

    @staticmethod
    def _session(db: Session, diver: User, *, expires_at: datetime, revoked_at: datetime | None = None) -> int:
        row = UserSession(
            user_id=diver.id,
            expires_at=expires_at,
            ip="203.0.113.7",
            user_agent="TestAgent/1.0",
            revoked_at=revoked_at,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        row_id = row.id
        db.expunge(row)
        return row_id

    @pytest.mark.asyncio
    async def test_an_expired_session_is_swept(self, db: Session, diver: User) -> None:
        row_id = self._session(db, diver, expires_at=datetime.now(UTC) - timedelta(minutes=1))

        await purge_expired_user_sessions({})

        assert db.get(UserSession, row_id) is None

    @pytest.mark.asyncio
    async def test_a_revoked_but_unexpired_session_is_swept_too(self, db: Session, diver: User) -> None:
        """Nothing ever reads a revoked row: the audit trail is what records that a session
        was revoked, and this table is not a second copy of it."""
        row_id = self._session(
            db, diver, expires_at=datetime.now(UTC) + timedelta(days=7), revoked_at=datetime.now(UTC)
        )

        await purge_expired_user_sessions({})

        assert db.get(UserSession, row_id) is None

    @pytest.mark.asyncio
    async def test_a_live_session_is_left_alone(self, db: Session, diver: User) -> None:
        row_id = self._session(db, diver, expires_at=datetime.now(UTC) + timedelta(days=7))

        await purge_expired_user_sessions({})

        assert db.get(UserSession, row_id) is not None


@needs_a_database
class TestErasureReachesBothArms:
    """The FK cascade takes the account-tied rows; the user-less ones carry an email and no
    `user_id`, so **the purge deletes those by email** - the identical second arm
    `authentication_request` already needed, and without which "audit rows are erased with
    the account" is false for exactly the rows that name an address.
    """

    @staticmethod
    def _event(db: Session, *, user: User | None = None, email: str | None = None) -> int:
        row = AuthAuditEvent(
            event_type=AuthEventType.SIGN_IN_SUCCEEDED if user else AuthEventType.AUTH_REQUEST_CREATED,
            ip="203.0.113.7",
            user_agent="TestAgent/1.0",
            user_id=user.id if user else None,
            email=email,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        row_id = row.id
        db.expunge(row)
        return row_id

    def test_the_by_email_delete_names_the_audit_table(self) -> None:
        """The statement's existence, asserted against the source: the cascade genuinely
        cannot reach these rows, so a purge that dropped this line would still pass every
        cascade test in the suite."""
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "src" / "app" / "core" / "worker" / "functions.py").read_text()

        assert "delete(AuthAuditEvent).where(AuthAuditEvent.email == email)" in source

    @pytest.mark.asyncio
    async def test_a_hard_delete_takes_the_account_tied_rows(self, db: Session, diver: User) -> None:
        from sqlalchemy import text

        row_id = self._event(db, user=diver)

        db.execute(text('DELETE FROM "user" WHERE id = :id'), {"id": diver.id})
        db.commit()

        assert db.get(AuthAuditEvent, row_id) is None

    @pytest.mark.asyncio
    async def test_a_hard_delete_cannot_reach_the_user_less_rows(self, db: Session, diver: User) -> None:
        """The finding the second arm exists for, stated as a fact about the schema rather
        than as a description of the fix."""
        from sqlalchemy import text

        address = diver.email
        row_id = self._event(db, email=address)

        db.execute(text('DELETE FROM "user" WHERE id = :id'), {"id": diver.id})
        db.commit()

        assert db.get(AuthAuditEvent, row_id) is not None, (
            "if the cascade reaches these, the purge's by-email arm has become unnecessary - re-read the design"
        )
        remaining = db.execute(
            select(AuthAuditEvent.id).where(AuthAuditEvent.email == address, AuthAuditEvent.user_id.is_(None))
        ).scalars()
        assert row_id in list(remaining)


@needs_a_database
class TestTheWriterAgainstPostgres:
    """`record_auth_event` against the real table, which is the only thing that can say the
    columns take what the call sites hand them."""

    @pytest.mark.asyncio
    async def test_a_bounded_context_is_accepted_at_full_width(self, async_db, diver: User) -> None:
        """A forged header truncated to the bound must still fit the column - the bound and
        the width come from the same constants for exactly this reason."""
        from src.app.core.utils.request_context import MAX_IP_LENGTH, MAX_USER_AGENT_LENGTH

        await record_auth_event(
            async_db,
            event_type=AuthEventType.LOGOUT,
            context=RequestContext(ip="7" * MAX_IP_LENGTH, user_agent="U" * MAX_USER_AGENT_LENGTH),
            user_id=diver.id,
        )

        stored = (
            (
                await async_db.execute(
                    select(AuthAuditEvent).where(AuthAuditEvent.user_id == diver.id).order_by(AuthAuditEvent.id.desc())
                )
            )
            .scalars()
            .first()
        )

        assert stored is not None
        assert len(stored.user_agent) == MAX_USER_AGENT_LENGTH
