"""Server-side sessions: the row, the `sid` that names it, and the three routes over it.

Split by what each half can actually prove. The route-level classes run over a mocked
session, because what they pin is a *decision* - 409 for your own session, 404 for
somebody else's, the count in the bulk response. Everything below `TestSessionStore` runs
against real Postgres, because what those pin is a `WHERE` clause: "revoked", "expired"
and "belongs to another account" are three predicates, and a mocked session evaluates
none of them.

The refresh half lives in `tests/test_auth_refresh.py` beside the rotation it continues.
What is here is the store it rotates against, and the one thing the fake there cannot
answer: that the lookup really does refuse a dead row.

The Postgres-backed classes skip silently without a reachable database - on a developer's
machine that means `POSTGRES_SERVER=localhost`, since `src/.env` points at the compose
hostname. CI sets it and fails the job if anything skips. See CONTRIBUTING.md.
"""

import uuid as uuid_pkg
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, Response
from jose import jwt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1.sessions import _LIST_LIMIT, erase_other_sessions, erase_session, read_sessions
from src.app.core.exceptions.http_exceptions import NotFoundException
from src.app.core.security import (
    ALGORITHM,
    SECRET_KEY,
    TokenType,
    create_access_token,
    create_refresh_token,
    token_session_id,
    verify_token,
)
from src.app.core.utils.request_context import RequestContext
from src.app.crud.crud_user_sessions import (
    MAX_LIVE_SESSIONS_PER_USER,
    evict_stalest_sessions,
    live_session_for,
    live_sessions_for_user,
    revoke_other_sessions,
    revoke_session,
)
from src.app.models.user import User
from src.app.models.user_session import UserSession
from src.app.schemas.user_session import UserSessionReadInternal, to_public_session
from src.app.services.auth_service import issue_tokens
from tests.conftest import db_available
from tests.helpers.mocks import FakeTokenBlacklist, fake_request

CONTEXT = RequestContext(ip="203.0.113.7", user_agent="Mozilla/5.0 (X11; Linux x86_64) TestAgent/1.0")

needs_a_database = pytest.mark.skipif(not db_available(), reason="No database connection available")


def _refresh_cookie(response: Response) -> str:
    """The refresh token out of `Set-Cookie`, the way the browser reads it.

    `issue_tokens` returns only the access token; the refresh half exists solely as an
    httpOnly cookie, so this is the only way at the value that carries the `sid`.
    """
    jar: SimpleCookie = SimpleCookie()
    for header in response.headers.getlist("set-cookie"):
        jar.load(header)
    return jar["refresh_token"].value


def _caller(**overrides: Any) -> dict[str, Any]:
    user = {"id": 7, "uuid": uuid_pkg.uuid4(), "email": "diver@example.com"}
    user.update(overrides)
    return user


def _internal(**overrides: Any) -> UserSessionReadInternal:
    now = datetime.now(UTC)
    row: dict[str, Any] = {
        "id": 1,
        "uuid": uuid7(),
        "user_id": 7,
        "expires_at": now + timedelta(days=7),
        "ip": "203.0.113.7",
        "user_agent": "TestAgent/1.0",
        "created_at": now,
        "last_used_at": now,
        "revoked_at": None,
    }
    row.update(overrides)
    return UserSessionReadInternal(**row)


class TestTheCurrentMarkerComesFromTheToken:
    """`current` is the one field on the response that is not a property of the row, which
    is exactly why this endpoint can never be cached: it varies by credential, and every
    cache key in this app is scoped to the *user*."""

    def test_the_requesting_session_is_marked(self) -> None:
        row = _internal()

        assert to_public_session(row, current_session_uuid=row.uuid).current is True

    def test_another_session_is_not(self) -> None:
        assert to_public_session(_internal(), current_session_uuid=uuid7()).current is False

    def test_a_token_with_no_sid_marks_nothing(self) -> None:
        """An access token minted before this feature carries no `sid`, and for its
        remaining minutes the honest answer is "I cannot tell" rather than a guess."""
        assert to_public_session(_internal(), current_session_uuid=None).current is False

    def test_the_public_shape_carries_nothing_token_derived(self) -> None:
        """The never-log rule's counterpart on the read side. A session row holds nothing
        token-derived by construction, and this is what would fail if one ever did."""
        public = to_public_session(_internal(), current_session_uuid=None)

        assert set(public.model_dump()) == {"uuid", "created_at", "last_used_at", "ip", "user_agent", "current"}


class TestRevokeOneSession:
    @pytest.mark.asyncio
    async def test_the_current_session_is_a_409_not_a_revoke(self, mock_db) -> None:
        """GitLab-style: ending your own session is what `POST /auth/logout` is, and it
        also has to clear the cookie and blacklist the presented pair. A revoke here would
        leave the browser holding a live access token and no way to refresh."""
        current = uuid7()

        with patch("src.app.api.v1.sessions.fetch_owned_or_raise", new_callable=AsyncMock) as owned:
            owned.return_value = _internal(uuid=current)

            with pytest.raises(HTTPException) as conflict:
                await erase_session(fake_request(), current, _caller(), current, mock_db)

        assert conflict.value.status_code == 409

    @pytest.mark.asyncio
    async def test_another_session_is_revoked(self, mock_db) -> None:
        other = uuid7()

        with (
            patch("src.app.api.v1.sessions.fetch_owned_or_raise", new_callable=AsyncMock) as owned,
            patch("src.app.api.v1.sessions.revoke_session", new_callable=AsyncMock) as revoke,
            patch("src.app.api.v1.sessions.record_auth_event", new_callable=AsyncMock),
        ):
            owned.return_value = _internal(uuid=other)

            await erase_session(fake_request(), other, _caller(), uuid7(), mock_db)

        revoke.assert_awaited_once()
        assert revoke.await_args.kwargs["session_uuid"] == other

    @pytest.mark.asyncio
    async def test_ownership_is_resolved_before_anything_else(self, mock_db) -> None:
        """Someone else's uuid is a 404 through `fetch_owned_or_raise`, and it has to be
        the *first* thing that happens - a current-session check that ran first would
        answer 409 for a uuid that happened to match, disclosing that it exists."""
        with (
            patch("src.app.api.v1.sessions.fetch_owned_or_raise", new_callable=AsyncMock) as owned,
            patch("src.app.api.v1.sessions.revoke_session", new_callable=AsyncMock) as revoke,
        ):
            owned.side_effect = NotFoundException("Session not found")

            with pytest.raises(NotFoundException):
                await erase_session(fake_request(), uuid7(), _caller(), uuid7(), mock_db)

        revoke.assert_not_called()


class TestRevokeOtherSessions:
    @pytest.mark.asyncio
    async def test_the_count_comes_back_in_the_response(self, mock_db) -> None:
        """The client confirms before it sends this, so the number of sessions actually
        ended cannot come from the dialog."""
        with (
            patch("src.app.api.v1.sessions.revoke_other_sessions", new_callable=AsyncMock) as revoke,
            patch("src.app.api.v1.sessions.record_auth_event", new_callable=AsyncMock),
        ):
            revoke.return_value = 3

            result = await erase_other_sessions(fake_request(), _caller(), uuid7(), mock_db)

        assert result.revoked == 3

    @pytest.mark.asyncio
    async def test_the_callers_own_session_is_spared(self, mock_db) -> None:
        mine = uuid7()

        with (
            patch("src.app.api.v1.sessions.revoke_other_sessions", new_callable=AsyncMock) as revoke,
            patch("src.app.api.v1.sessions.record_auth_event", new_callable=AsyncMock),
        ):
            revoke.return_value = 0

            await erase_other_sessions(fake_request(), _caller(), mine, mock_db)

        assert revoke.await_args.kwargs["except_uuid"] == mine

    @pytest.mark.asyncio
    async def test_one_event_covers_the_whole_action(self, mock_db) -> None:
        """Not one per row: the rows are the event's subject, and a per-row event would
        turn one deliberate act into ninety-nine entries."""
        with (
            patch("src.app.api.v1.sessions.revoke_other_sessions", new_callable=AsyncMock) as revoke,
            patch("src.app.api.v1.sessions.record_auth_event", new_callable=AsyncMock) as event,
        ):
            revoke.return_value = 12

            await erase_other_sessions(fake_request(), _caller(), uuid7(), mock_db)

        event.assert_awaited_once()


@needs_a_database
class TestSessionStore:
    """The predicates, against the database that evaluates them."""

    @staticmethod
    def _session(
        db: Session,
        diver: User,
        *,
        last_used_at: datetime | None = None,
        expires_at: datetime | None = None,
        revoked_at: datetime | None = None,
    ) -> UserSession:
        now = datetime.now(UTC)
        row = UserSession(
            user_id=diver.id,
            expires_at=expires_at or now + timedelta(days=7),
            ip="203.0.113.7",
            user_agent="TestAgent/1.0",
            last_used_at=last_used_at or now,
            revoked_at=revoked_at,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @pytest.mark.asyncio
    async def test_a_live_session_resolves(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        row = self._session(db, diver)

        assert await live_session_for(async_db, session_uuid=row.uuid, user_id=diver.id) is not None

    @pytest.mark.asyncio
    async def test_a_revoked_session_does_not(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        row = self._session(db, diver, revoked_at=datetime.now(UTC))

        assert await live_session_for(async_db, session_uuid=row.uuid, user_id=diver.id) is None

    @pytest.mark.asyncio
    async def test_an_expired_session_does_not(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        row = self._session(db, diver, expires_at=datetime.now(UTC) - timedelta(minutes=1))

        assert await live_session_for(async_db, session_uuid=row.uuid, user_id=diver.id) is None

    @pytest.mark.asyncio
    async def test_another_accounts_session_does_not(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        """The `user_id` clause is what stops a stolen `sid` refreshing against somebody
        else's row - and it is invisible in any test that only ever uses one account."""
        row = self._session(db, other_diver)

        assert await live_session_for(async_db, session_uuid=row.uuid, user_id=diver.id) is None

    @pytest.mark.asyncio
    async def test_the_list_is_live_rows_newest_used_first(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        now = datetime.now(UTC)
        older = self._session(db, diver, last_used_at=now - timedelta(hours=2))
        newer = self._session(db, diver, last_used_at=now)
        self._session(db, diver, revoked_at=now)
        self._session(db, diver, expires_at=now - timedelta(minutes=1))

        rows = await live_sessions_for_user(async_db, user_id=diver.id, limit=50)

        assert [row.uuid for row in rows] == [newer.uuid, older.uuid]

    @pytest.mark.asyncio
    async def test_revoking_is_idempotent(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """Unlike the hard-deleting resources, whose second `DELETE` is a 404: the row is
        not what is being removed, so re-revoking one the caller owns changes nothing."""
        row = self._session(db, diver)

        await revoke_session(async_db, session_uuid=row.uuid)
        await async_db.commit()
        first = (await async_db.execute(select(UserSession.revoked_at).where(UserSession.id == row.id))).scalar_one()

        await revoke_session(async_db, session_uuid=row.uuid)
        await async_db.commit()
        second = (await async_db.execute(select(UserSession.revoked_at).where(UserSession.id == row.id))).scalar_one()

        assert first is not None
        assert second == first, "a second revoke moved the timestamp the first one set"

    @pytest.mark.asyncio
    async def test_revoke_others_spares_the_named_session_and_the_neighbours(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        mine = self._session(db, diver)
        also_mine = self._session(db, diver)
        theirs = self._session(db, other_diver)

        revoked = await revoke_other_sessions(async_db, user_id=diver.id, except_uuid=mine.uuid)
        await async_db.commit()

        assert revoked == 1
        assert await live_session_for(async_db, session_uuid=mine.uuid, user_id=diver.id) is not None
        assert await live_session_for(async_db, session_uuid=also_mine.uuid, user_id=diver.id) is None
        assert await live_session_for(async_db, session_uuid=theirs.uuid, user_id=other_diver.id) is not None

    @pytest.mark.asyncio
    async def test_revoke_others_with_no_current_session_signs_everything_out(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """An access token minted before this feature names no session to spare, so there
        is nothing to exclude and the caller goes with the rest."""
        self._session(db, diver)
        self._session(db, diver)

        revoked = await revoke_other_sessions(async_db, user_id=diver.id, except_uuid=None)
        await async_db.commit()

        assert revoked == 2


@needs_a_database
class TestTheCapEvictsTheStalest:
    """**By `last_used_at`, not `created_at`** - the deliberate departure from GitLab's
    documented oldest-deleted. A browser in daily use must never be signed out to make room
    while a dormant one keeps its slot, and eviction by creation order does exactly that to
    the oldest - often busiest - device.
    """

    @staticmethod
    def _session(db: Session, diver: User, *, created_at: datetime, last_used_at: datetime) -> UserSession:
        row = UserSession(
            user_id=diver.id,
            expires_at=datetime.now(UTC) + timedelta(days=7),
            ip="203.0.113.7",
            user_agent="TestAgent/1.0",
            created_at=created_at,
            last_used_at=last_used_at,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @pytest.mark.asyncio
    async def test_the_least_recently_used_row_goes_not_the_oldest(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        now = datetime.now(UTC)
        # Created first and still in daily use - the row GitLab's rule would have taken.
        busy_and_old = self._session(db, diver, created_at=now - timedelta(days=300), last_used_at=now)
        # Created yesterday and untouched since.
        idle_and_new = self._session(
            db, diver, created_at=now - timedelta(days=1), last_used_at=now - timedelta(days=1)
        )

        evicted = await evict_stalest_sessions(async_db, user_id=diver.id, keep=1)
        await async_db.commit()

        assert evicted == 1
        assert await live_session_for(async_db, session_uuid=busy_and_old.uuid, user_id=diver.id) is not None
        assert await live_session_for(async_db, session_uuid=idle_and_new.uuid, user_id=diver.id) is None

    @pytest.mark.asyncio
    async def test_it_only_ever_touches_the_one_account(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        now = datetime.now(UTC)
        self._session(db, diver, created_at=now, last_used_at=now - timedelta(days=9))
        theirs = self._session(db, other_diver, created_at=now, last_used_at=now - timedelta(days=99))

        await evict_stalest_sessions(async_db, user_id=diver.id, keep=0)
        await async_db.commit()

        assert await live_session_for(async_db, session_uuid=theirs.uuid, user_id=other_diver.id) is not None

    @pytest.mark.asyncio
    async def test_minting_past_the_cap_lands_the_account_at_it(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """Driven through `issue_tokens`, because the eviction is only correct in the place
        it actually runs: before the insert, keeping one slot free, so the row being minted
        is never a candidate for its own eviction."""
        now = datetime.now(UTC)
        for index in range(MAX_LIVE_SESSIONS_PER_USER):
            self._session(db, diver, created_at=now, last_used_at=now - timedelta(minutes=index))

        with patch("src.app.core.security.crud_token_blacklist", FakeTokenBlacklist()):
            await issue_tokens(Response(), diver.uuid, db=async_db, context=CONTEXT, user_id=diver.id)

        live = (
            await async_db.execute(
                select(func.count())
                .select_from(UserSession)
                .where(
                    UserSession.user_id == diver.id,
                    UserSession.revoked_at.is_(None),
                    UserSession.expires_at > datetime.now(UTC),
                )
            )
        ).scalar_one()

        assert live == MAX_LIVE_SESSIONS_PER_USER

    @pytest.mark.asyncio
    async def test_the_list_limit_strictly_exceeds_the_cap(self) -> None:
        """A session nobody can see is a session nobody can revoke. Eviction is best-effort
        under concurrency, so the list has to read past the cap rather than up to it."""
        assert _LIST_LIMIT > MAX_LIVE_SESSIONS_PER_USER


@needs_a_database
class TestEveryMintingPathCreatesExactlyOneSession:
    """Parametrized over what the `issue_tokens` sweep finds, reduced to the two shapes it
    resolves to: a mint that starts a session, and a mint that continues one.

    The four call sites (`_start_onboarding_or_sign_in`, `POST /auth/complete`,
    `POST /auth/restore`, `POST /auth/refresh`) all reach this one function, which is what
    makes "exactly one session per minting path" a property of the code rather than of four
    sites remembering - so this is where it is pinned.
    """

    @staticmethod
    async def _live_count(async_db: AsyncSession, user_id: int) -> int:
        count = await async_db.execute(
            select(func.count())
            .select_from(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > datetime.now(UTC),
            )
        )
        return int(count.scalar_one())

    @pytest.mark.asyncio
    async def test_a_mint_with_no_session_starts_exactly_one(self, async_db: AsyncSession, diver: User) -> None:
        with patch("src.app.core.security.crud_token_blacklist", FakeTokenBlacklist()):
            await issue_tokens(Response(), diver.uuid, db=async_db, context=CONTEXT, user_id=diver.id)

        assert await self._live_count(async_db, diver.id) == 1

    @pytest.mark.asyncio
    async def test_the_row_records_where_the_request_came_from(self, async_db: AsyncSession, diver: User) -> None:
        with patch("src.app.core.security.crud_token_blacklist", FakeTokenBlacklist()):
            await issue_tokens(Response(), diver.uuid, db=async_db, context=CONTEXT, user_id=diver.id)

        rows = await live_sessions_for_user(async_db, user_id=diver.id, limit=10)

        assert rows[0].ip == CONTEXT.ip
        assert rows[0].user_agent == CONTEXT.user_agent

    @pytest.mark.asyncio
    async def test_both_tokens_carry_the_new_sessions_sid(self, async_db: AsyncSession, diver: User) -> None:
        response = Response()

        with patch("src.app.core.security.crud_token_blacklist", FakeTokenBlacklist()):
            tokens = await issue_tokens(response, diver.uuid, db=async_db, context=CONTEXT, user_id=diver.id)

        rows = await live_sessions_for_user(async_db, user_id=diver.id, limit=10)
        refresh_cookie = _refresh_cookie(response)

        assert token_session_id(tokens["access_token"]) == rows[0].uuid
        assert token_session_id(refresh_cookie) == rows[0].uuid

    @pytest.mark.asyncio
    async def test_continuing_a_session_starts_no_second_one(self, async_db: AsyncSession, diver: User) -> None:
        with patch("src.app.core.security.crud_token_blacklist", FakeTokenBlacklist()):
            first = Response()
            await issue_tokens(first, diver.uuid, db=async_db, context=CONTEXT, user_id=diver.id)
            existing = token_session_id(_refresh_cookie(first))
            assert existing is not None

            await issue_tokens(
                Response(), diver.uuid, db=async_db, context=CONTEXT, user_id=diver.id, session_uuid=existing
            )

        assert await self._live_count(async_db, diver.id) == 1

    @pytest.mark.asyncio
    async def test_continuing_stamps_last_used_and_slides_the_expiry(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """The sliding *inactivity* window the privacy copy already describes: each refresh
        restarts the clock, so a browser in daily use never expires."""
        stale = datetime.now(UTC) - timedelta(days=3)
        row = UserSession(
            user_id=diver.id,
            expires_at=stale + timedelta(days=7),
            ip="203.0.113.7",
            user_agent="TestAgent/1.0",
            last_used_at=stale,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        before_used, before_expiry, row_id = row.last_used_at, row.expires_at, row.id

        with patch("src.app.core.security.crud_token_blacklist", FakeTokenBlacklist()):
            await issue_tokens(
                Response(), diver.uuid, db=async_db, context=CONTEXT, user_id=diver.id, session_uuid=row.uuid
            )

        after = (
            await async_db.execute(
                select(UserSession.last_used_at, UserSession.expires_at).where(UserSession.id == row_id)
            )
        ).one()

        assert after.last_used_at > before_used
        assert after.expires_at > before_expiry


class TestTheSidClaim:
    """`sid` and `jti` do different jobs, and neither replaces the other."""

    @pytest.mark.asyncio
    async def test_a_pre_upgrade_token_verifies_but_names_no_session(self, mock_db) -> None:
        """The whole of the upgrade story: an old cookie still decodes and still verifies -
        it simply names no session, which is what `/auth/refresh` refuses. One global
        sign-out, no shim."""
        with patch("src.app.core.security.crud_token_blacklist", FakeTokenBlacklist()):
            old = await create_refresh_token(data={"sub": str(uuid7())})
            data = await verify_token(old, TokenType.REFRESH, mock_db)

        assert data is not None
        assert data.session_uuid is None

    @pytest.mark.asyncio
    async def test_the_claim_rides_both_halves_of_the_pair(self, mock_db) -> None:
        session_uuid = uuid7()

        access = await create_access_token(data={"sub": str(uuid7())}, session_uuid=session_uuid)
        refresh = await create_refresh_token(data={"sub": str(uuid7())}, session_uuid=session_uuid)

        assert token_session_id(access) == session_uuid
        assert token_session_id(refresh) == session_uuid

    @pytest.mark.asyncio
    async def test_two_tokens_for_one_session_still_differ(self, mock_db) -> None:
        """`sid` is shared across a session's whole lineage, so it cannot also be what makes
        one issuance distinct from the next - that is still `jti`'s job, and blacklisting by
        value depends on it."""
        session_uuid = uuid7()
        subject = str(uuid7())

        first = await create_refresh_token(data={"sub": subject}, session_uuid=session_uuid)
        second = await create_refresh_token(data={"sub": subject}, session_uuid=session_uuid)

        assert first != second
        payloads = [
            jwt.decode(token, SECRET_KEY.get_secret_value(), algorithms=[ALGORITHM]) for token in (first, second)
        ]
        assert payloads[0]["jti"] != payloads[1]["jti"]
        assert payloads[0]["sid"] == payloads[1]["sid"]

    def test_an_unparseable_sid_reads_as_absent(self) -> None:
        """Not reachable through anything this app signs, and tolerated rather than raised
        for the reason `verify_token` tolerates a non-uuid `sub`: an escaping `ValueError`
        on a decode path is a 500 where a 401 belongs."""
        forged = jwt.encode(
            {"sub": str(uuid7()), "sid": "not-a-uuid", "token_type": TokenType.REFRESH},
            SECRET_KEY.get_secret_value(),
            algorithm=ALGORITHM,
        )

        assert token_session_id(forged) is None


class TestTheListIsNotCached:
    """Not a staleness trade like `GET /user/passkeys`, a correctness one: the response
    varies by credential and every cache key here is scoped to the user, so a cached entry
    would serve one device's "This device" marker to another."""

    @pytest.mark.asyncio
    async def test_the_route_reads_straight_through(self, mock_db) -> None:
        with patch("src.app.api.v1.sessions.live_sessions_for_user", new_callable=AsyncMock) as read:
            read.return_value = [_internal()]

            await read_sessions(_caller(), uuid7(), mock_db)

        read.assert_awaited_once()

    def test_no_cache_decorator_anywhere_in_the_module(self) -> None:
        """Asserted against the source, because the failure mode is somebody adding the
        decorator that every other owned-resource read has - it would look like consistency
        and would be an IDOR-shaped bug on the `current` flag."""
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "src" / "app" / "api" / "v1" / "sessions.py").read_text()

        assert "@cache" not in source
        assert "OwnedResourceCache" not in source
