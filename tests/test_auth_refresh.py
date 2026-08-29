"""Tests for the `POST /auth/refresh` endpoint (now part of `api.v1.auth` - see
`tests/test_auth.py` for the unified email/Google/onboarding flow, and `DECISIONS.md`
for why `/refresh`/`/logout` moved under `/auth`, and for why the presented refresh
token is rotated rather than reused).

Mostly unit tests over a mocked session; the last class is Postgres-backed, because the
liveness check the endpoint makes is a real query and a fake cannot fail the way it can.
"""

import logging
import uuid as uuid_pkg
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import Response

from src.app.api.v1.auth import _elapsed, refresh_access_token
from src.app.core.exceptions.http_exceptions import UnauthorizedException
from src.app.core.schemas import TokenData
from src.app.core.utils.request_context import RequestContext
from src.app.schemas.user_session import UserSessionReadInternal
from src.app.services.auth_service import issue_tokens
from tests.conftest import db_available
from tests.helpers.mocks import FakeTokenBlacklist, FrozenSecurityClock

USER_UUID = uuid_pkg.uuid4()
AUTH_LOGGER = "src.app.api.v1.auth"

# Every token mint now records the device it was minted from, onto the session row.
CONTEXT = RequestContext(ip="203.0.113.7", user_agent="Mozilla/5.0 (X11; Linux x86_64) TestAgent/1.0")


def _request(cookies: dict[str, str]) -> Mock:
    """This module's own double, because the one in `tests.helpers.mocks` carries no
    cookies and the cookie is the whole credential here.

    `headers` is a real mapping for the reason that one documents: `RequestContext`
    reads the User-Agent off it and slices the result, which a bare child mock turns into
    a `TypeError` raised from inside the endpoint.
    """
    request = Mock()
    request.cookies = cookies
    request.client.host = "203.0.113.7"
    request.headers = {"user-agent": CONTEXT.user_agent}
    return request


def _refresh_cookie(response: Response) -> str:
    """Reads the refresh token back out of `Set-Cookie`, the way the browser will.

    `issue_tokens` returns only the access token - the refresh token exists solely as an
    httpOnly cookie on the response, so this is the only way to get at the value the next
    `/auth/refresh` will present.
    """
    jar: SimpleCookie = SimpleCookie()
    for header in response.headers.getlist("set-cookie"):
        jar.load(header)
    return jar["refresh_token"].value


USER_ID = 1

# The `sid` a verified refresh token carries. A `TokenData` without one is a token minted
# before sessions existed, which is its own case below.
SESSION_UUID = uuid_pkg.uuid4()


class FakeUsers:
    """Stand-in for `crud_users` holding exactly one account, applied the way FastCRUD
    applies its keyword filters: every one has to match.

    An unknown column compares against `None` and so fails, which is the point - a lookup
    that filtered on a field the row doesn't carry would quietly answer "no such account"
    against real Postgres too, and this fake refuses it rather than passing.

    `get` alongside `exists` because the endpoint stopped asking whether the account exists
    and started asking *which* one it is: the session lookup that follows needs the integer
    id, so a bare boolean is no longer enough to serve this path.
    """

    def __init__(self, user_uuid: uuid_pkg.UUID, is_deleted: bool = False) -> None:
        self.row: dict[str, Any] = {"id": USER_ID, "uuid": user_uuid, "is_deleted": is_deleted}
        self.filters: list[dict[str, Any]] = []

    def _matches(self, filters: dict[str, Any]) -> bool:
        self.filters.append(filters)
        return all(self.row.get(field) == value for field, value in filters.items())

    async def exists(self, db: Any, **filters: Any) -> bool:
        return self._matches(filters)

    async def get(self, db: Any, **filters: Any) -> dict[str, Any] | None:
        return self.row if self._matches(filters) else None


class FakeSessions:
    """The live-session lookup `POST /auth/refresh` makes, answering for whatever `sid` it
    is handed.

    Deliberately permissive: these tests are about *rotation*, and a store that refused
    would fail every one of them for a reason that has nothing to do with what they pin.
    The cases where the lookup must answer `None` - revoked, expired, another account's -
    are pinned against real Postgres in `test_sessions.py`, which is the only place they
    mean anything.
    """

    def __init__(self) -> None:
        self.asked: list[uuid_pkg.UUID] = []

    async def __call__(self, db: Any, *, session_uuid: uuid_pkg.UUID, user_id: int) -> UserSessionReadInternal:
        self.asked.append(session_uuid)
        now = datetime.now(UTC)
        return UserSessionReadInternal(
            id=1,
            uuid=session_uuid,
            user_id=user_id,
            expires_at=now + timedelta(days=7),
            ip="203.0.113.7",
            user_agent="TestAgent/1.0",
            created_at=now,
            last_used_at=now,
            revoked_at=None,
        )


class SignedInAccount:
    """Refreshing is now a question about the account and its session, not just about the
    token, so every test that expects a *successful* exchange has to supply both.

    `mock_db` would supply them by accident: a spec'd `AsyncSession` answers every call
    with another mock, which FastCRUD's `exists` reads as a row - and leaves an
    un-awaited coroutine behind while doing it. Tests that pass because a mock is truthy
    would also pass with the checks deleted, so both are stated here instead.
    """

    @pytest.fixture(autouse=True)
    def _live_account(self):
        with (
            patch("src.app.api.v1.auth.crud_users", FakeUsers(USER_UUID)),
            patch("src.app.api.v1.auth.live_session_for", FakeSessions()),
        ):
            yield


class TestRefreshAccessToken(SignedInAccount):
    @pytest.mark.asyncio
    async def test_missing_cookie_raises_unauthorized(self, mock_db):
        with pytest.raises(UnauthorizedException, match="Refresh token missing."):
            await refresh_access_token(_request({}), Mock(), mock_db)

    @pytest.mark.asyncio
    async def test_invalid_refresh_token_raises_unauthorized(self, mock_db):
        with (
            # The failure path asks the blacklist whether this token was one we revoked
            # (see `_warn_if_revoked`), so it needs somewhere to ask even when the answer
            # is "never seen it" - `mock_db` alone can't answer a real query.
            patch("src.app.core.security.crud_token_blacklist", FakeTokenBlacklist()),
            patch("src.app.api.v1.auth.verify_token", new_callable=AsyncMock) as mock_verify,
        ):
            mock_verify.return_value = None

            with pytest.raises(UnauthorizedException, match="Invalid refresh token."):
                await refresh_access_token(_request({"refresh_token": "bad-token"}), Mock(), mock_db)

    @pytest.mark.asyncio
    async def test_invalid_refresh_token_is_not_blacklisted(self, mock_db):
        """A token that didn't verify must not be written to the blacklist table - that
        would let anyone grow the table by posting garbage cookies.
        """
        with (
            patch("src.app.core.security.crud_token_blacklist", FakeTokenBlacklist()),
            patch("src.app.api.v1.auth.verify_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.api.v1.auth.blacklist_token", new_callable=AsyncMock) as mock_blacklist,
        ):
            mock_verify.return_value = None

            with pytest.raises(UnauthorizedException):
                await refresh_access_token(_request({"refresh_token": "bad-token"}), Mock(), mock_db)

            mock_blacklist.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_refresh_token_returns_new_access_token(self, mock_db):
        response = Mock()

        with (
            patch("src.app.api.v1.auth.verify_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.api.v1.auth.blacklist_token", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.issue_tokens", new_callable=AsyncMock) as mock_issue,
        ):
            mock_verify.return_value = TokenData(user_uuid=USER_UUID, session_uuid=SESSION_UUID)
            mock_issue.return_value = {"access_token": "new-access-token", "token_type": "bearer"}

            result = await refresh_access_token(_request({"refresh_token": "good-token"}), response, mock_db)

            assert result == {"access_token": "new-access-token", "token_type": "bearer"}
            # A fresh refresh cookie is set on the same response, not just an access token.
            # The replacement carries the presented token's subject through unchanged -
            # which is safe only because that subject is an immutable uuid.
            # The replacement continues the presented token's session rather than
            # starting a new one - `sid` is what survives rotation, and passing it back
            # in is the whole mechanism.
            assert mock_issue.await_args.args == (response, USER_UUID)
            assert mock_issue.await_args.kwargs["session_uuid"] == SESSION_UUID
            assert mock_issue.await_args.kwargs["user_id"] == USER_ID

    @pytest.mark.asyncio
    async def test_presented_refresh_token_is_rotated_out(self, mock_db):
        """The whole point of rotation: the cookie that was handed in is spent, so a
        replay of the same value fails even though it hasn't reached its `exp` yet.
        """
        with (
            patch("src.app.api.v1.auth.verify_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.api.v1.auth.blacklist_token", new_callable=AsyncMock) as mock_blacklist,
            patch("src.app.api.v1.auth.issue_tokens", new_callable=AsyncMock) as mock_issue,
        ):
            mock_verify.return_value = TokenData(user_uuid=USER_UUID, session_uuid=SESSION_UUID)
            mock_issue.return_value = {"access_token": "new-access-token", "token_type": "bearer"}

            await refresh_access_token(_request({"refresh_token": "good-token"}), Mock(), mock_db)

            mock_blacklist.assert_called_once_with("good-token", mock_db)


class TestRefreshRotationWithRealTokens(SignedInAccount):
    """Rotation driven end to end over the real helpers: tokens are genuinely minted,
    verified and blacklisted, with only the blacklist *table* swapped for an in-memory
    set.

    The tests above mock `verify_token`/`blacklist_token`/`issue_tokens` out, which is
    precisely how the one-second collision survived them - every assertion held against
    tokens that were never actually minted, so the endpoint handing back a token it had
    just revoked was invisible. These run with the clock frozen, so *every* token in the
    exchange shares one `exp`.
    """

    @pytest.mark.asyncio
    async def test_rotation_hands_back_a_token_that_is_not_already_revoked(self, mock_db):
        """The reported bug: sign in, refresh once, and the replacement cookie was the
        token just blacklisted - so the *next* refresh 401'd and the user landed on
        /signin for no reason they could see.
        """
        blacklist = FakeTokenBlacklist()

        with (
            patch("src.app.core.security.crud_token_blacklist", blacklist),
            patch("src.app.core.security.datetime", FrozenSecurityClock),
        ):
            sign_in = Response()
            await issue_tokens(sign_in, USER_UUID, db=mock_db, context=CONTEXT, user_id=USER_ID)
            original = _refresh_cookie(sign_in)

            rotated_response = Response()
            await refresh_access_token(_request({"refresh_token": original}), rotated_response, mock_db)
            rotated = _refresh_cookie(rotated_response)

            assert rotated != original
            assert rotated not in blacklist.tokens

            # The replacement has to survive being presented, which is what actually
            # failed: the second refresh is the one the user saw as a random logout.
            second_rotation = Response()
            result = await refresh_access_token(_request({"refresh_token": rotated}), second_rotation, mock_db)

            assert result["token_type"] == "bearer"
            assert _refresh_cookie(second_rotation) not in (original, rotated)

    @pytest.mark.asyncio
    async def test_spent_refresh_token_cannot_be_reused(self, mock_db):
        """The other half of rotation: replaying the cookie that was already exchanged
        fails, even though it is nowhere near its `exp`.
        """
        blacklist = FakeTokenBlacklist()

        with (
            patch("src.app.core.security.crud_token_blacklist", blacklist),
            patch("src.app.core.security.datetime", FrozenSecurityClock),
        ):
            sign_in = Response()
            await issue_tokens(sign_in, USER_UUID, db=mock_db, context=CONTEXT, user_id=USER_ID)
            original = _refresh_cookie(sign_in)

            await refresh_access_token(_request({"refresh_token": original}), Response(), mock_db)

            with pytest.raises(UnauthorizedException, match="Invalid refresh token."):
                await refresh_access_token(_request({"refresh_token": original}), Response(), mock_db)

    @pytest.mark.asyncio
    async def test_concurrent_sign_ins_issue_independent_sessions(self, mock_db):
        """Two sign-ins for one account in the same second are two sessions: refreshing
        (and so revoking) one must leave the other usable. Identical tokens made them one
        session that either could end.
        """
        blacklist = FakeTokenBlacklist()

        with (
            patch("src.app.core.security.crud_token_blacklist", blacklist),
            patch("src.app.core.security.datetime", FrozenSecurityClock),
        ):
            first_device, second_device = Response(), Response()
            first_tokens = await issue_tokens(first_device, USER_UUID, db=mock_db, context=CONTEXT, user_id=USER_ID)
            second_tokens = await issue_tokens(second_device, USER_UUID, db=mock_db, context=CONTEXT, user_id=USER_ID)

            assert first_tokens["access_token"] != second_tokens["access_token"]

            await refresh_access_token(_request({"refresh_token": _refresh_cookie(first_device)}), Response(), mock_db)

            # The second device never refreshed, so its cookie is untouched.
            still_valid = Response()
            second_cookie = _refresh_cookie(second_device)
            await refresh_access_token(_request({"refresh_token": second_cookie}), still_valid, mock_db)

            assert _refresh_cookie(still_valid) not in blacklist.tokens


class TestRefreshTokenReuseLogging(SignedInAccount):
    """Presenting a refresh token that was already spent is the strongest evidence this
    app gets that a cookie has been stolen, and until it was logged it produced nothing an
    operator could ever see - `verify_token` answers `None` for a revoked token and for
    garbage alike.

    Two things are load-bearing and both are asserted here: the level (`WARNING`, because
    nothing configures logging below it - see `_warn_if_revoked`) and the silence on a
    malformed token, which is noise rather than a security event.
    """

    async def _spent_token(self, mock_db) -> str:
        """Sign in and refresh once, returning the cookie that was spent doing so."""
        sign_in = Response()
        await issue_tokens(sign_in, USER_UUID, db=mock_db, context=CONTEXT, user_id=USER_ID)
        spent = _refresh_cookie(sign_in)
        await refresh_access_token(_request({"refresh_token": spent}), Response(), mock_db)
        return spent

    @pytest.mark.asyncio
    async def test_reuse_logs_a_warning_naming_the_account(self, mock_db, caplog):
        blacklist = FakeTokenBlacklist()

        with (
            patch("src.app.core.security.crud_token_blacklist", blacklist),
            patch("src.app.core.security.datetime", FrozenSecurityClock),
        ):
            spent = await self._spent_token(mock_db)

            # Captured from `DEBUG` up on purpose: capturing at `WARNING` would pass just
            # as well against an `info` call that a real deployment never prints.
            with caplog.at_level(logging.DEBUG, logger=AUTH_LOGGER):
                with pytest.raises(UnauthorizedException, match="Invalid refresh token."):
                    await refresh_access_token(_request({"refresh_token": spent}), Response(), mock_db)

        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.WARNING
        message = caplog.records[0].getMessage()
        assert str(USER_UUID) in message
        assert "after it was revoked" in message

    @pytest.mark.asyncio
    async def test_malformed_token_logs_nothing(self, mock_db, caplog):
        """A cookie that was never a token this server issued is noise. Logging it would
        let anyone fill the operator's log by posting garbage, and would drown the one
        line that means something.
        """
        blacklist = FakeTokenBlacklist()

        with (
            patch("src.app.core.security.crud_token_blacklist", blacklist),
            caplog.at_level(logging.DEBUG, logger=AUTH_LOGGER),
            pytest.raises(UnauthorizedException, match="Invalid refresh token."),
        ):
            await refresh_access_token(_request({"refresh_token": "not-a-jwt"}), Response(), mock_db)

        assert caplog.records == []

    @pytest.mark.asyncio
    async def test_the_response_is_identical_either_way(self, mock_db):
        """The log is the *only* place the two cases differ. If the 401 differed, it would
        be an oracle for whether a given cookie was ever a real token.
        """
        blacklist = FakeTokenBlacklist()

        with (
            patch("src.app.core.security.crud_token_blacklist", blacklist),
            patch("src.app.core.security.datetime", FrozenSecurityClock),
        ):
            spent = await self._spent_token(mock_db)

            with pytest.raises(UnauthorizedException) as reused:
                await refresh_access_token(_request({"refresh_token": spent}), Response(), mock_db)

            with pytest.raises(UnauthorizedException) as garbage:
                await refresh_access_token(_request({"refresh_token": "not-a-jwt"}), Response(), mock_db)

        assert reused.value.detail == garbage.value.detail
        assert reused.value.status_code == garbage.value.status_code

    @pytest.mark.asyncio
    async def test_revocation_is_timestamped_when_the_token_is_spent(self, mock_db):
        """`expires_at` is the token's own `exp`, i.e. when it was *issued* - so without
        `revoked_at` there is nothing to measure the replay against.
        """
        blacklist = FakeTokenBlacklist()

        with (
            patch("src.app.core.security.crud_token_blacklist", blacklist),
            patch("src.app.core.security.datetime", FrozenSecurityClock),
        ):
            spent = await self._spent_token(mock_db)

        entry = blacklist.entries[spent]
        assert entry.revoked_at == FrozenSecurityClock.now(UTC)
        assert entry.revoked_at != entry.expires_at

    def test_elapsed_stays_readable_at_both_ends_of_the_range(self):
        """Sub-second resolution is what separates the documented two-tab race from a
        replay, and a replay can land days later - where a bare seconds count says nothing
        at a glance.
        """
        now = datetime.now(UTC)

        assert _elapsed(now - timedelta(milliseconds=12)).endswith("s")
        assert float(_elapsed(now - timedelta(milliseconds=12)).rstrip("s")) < 1
        assert "day" in _elapsed(now - timedelta(days=3))


class TestRefreshRequiresALiveAccount:
    """The subject of a refresh token is an immutable uuid, which stops it naming a
    *different* account - but nothing stopped it naming a **deleted** one.

    `verify_token` never touches the `user` table and neither does `issue_tokens`, so a
    cookie held by any device other than the one that called `DELETE /user` kept rotating
    itself indefinitely: that call blacklists only the two tokens presented to it. Every
    read 401s through `get_current_user`'s `is_deleted=False` filter from the same
    instant; this endpoint was the exception, and the one that mattered, because it is
    what keeps a session alive.
    """

    @pytest.mark.asyncio
    async def test_a_deleted_account_cannot_refresh(self, mock_db):
        with (
            patch("src.app.api.v1.auth.verify_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.api.v1.auth.crud_users", FakeUsers(USER_UUID, is_deleted=True)),
        ):
            mock_verify.return_value = TokenData(user_uuid=USER_UUID, session_uuid=SESSION_UUID)

            with pytest.raises(UnauthorizedException, match="Invalid refresh token."):
                await refresh_access_token(_request({"refresh_token": "good-token"}), Mock(), mock_db)

    @pytest.mark.asyncio
    async def test_a_subject_with_no_row_at_all_cannot_refresh(self, mock_db):
        """The account purge removes the row outright, and a pre-purge cookie names a uuid
        nothing resolves. Same answer as a deleted row.
        """
        with (
            patch("src.app.api.v1.auth.verify_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.api.v1.auth.crud_users", FakeUsers(uuid_pkg.uuid4())),
        ):
            mock_verify.return_value = TokenData(user_uuid=USER_UUID, session_uuid=SESSION_UUID)

            with pytest.raises(UnauthorizedException, match="Invalid refresh token."):
                await refresh_access_token(_request({"refresh_token": "good-token"}), Mock(), mock_db)

    @pytest.mark.asyncio
    async def test_the_account_is_looked_up_by_the_token_subject(self, mock_db):
        """Both halves of the filter are load-bearing and neither is visible in a passing
        happy-path test: the uuid has to be the one the *token* named, and `is_deleted`
        has to be there at all.
        """
        users = FakeUsers(USER_UUID)

        with (
            patch("src.app.api.v1.auth.verify_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.api.v1.auth.crud_users", users),
            patch("src.app.api.v1.auth.live_session_for", FakeSessions()),
            patch("src.app.api.v1.auth.blacklist_token", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.issue_tokens", new_callable=AsyncMock),
        ):
            mock_verify.return_value = TokenData(user_uuid=USER_UUID, session_uuid=SESSION_UUID)

            await refresh_access_token(_request({"refresh_token": "good-token"}), Mock(), mock_db)

        assert users.filters == [{"uuid": USER_UUID, "is_deleted": False}]

    @pytest.mark.asyncio
    async def test_a_rejected_token_is_not_spent(self, mock_db):
        """The 401 writes nothing, for the same reason the malformed-token 401 doesn't -
        and so a soft delete that gets reversed leaves the account's other sessions
        working, having made them inert rather than destroyed them.
        """
        with (
            patch("src.app.api.v1.auth.verify_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.api.v1.auth.crud_users", FakeUsers(USER_UUID, is_deleted=True)),
            patch("src.app.api.v1.auth.blacklist_token", new_callable=AsyncMock) as mock_blacklist,
            patch("src.app.api.v1.auth.issue_tokens", new_callable=AsyncMock) as mock_issue,
        ):
            mock_verify.return_value = TokenData(user_uuid=USER_UUID, session_uuid=SESSION_UUID)

            with pytest.raises(UnauthorizedException):
                await refresh_access_token(_request({"refresh_token": "good-token"}), Mock(), mock_db)

            mock_blacklist.assert_not_called()
            mock_issue.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_response_says_nothing_about_why(self, mock_db):
        """A distinguishable 401 would turn the endpoint into an oracle for whether a
        given uuid ever had an account - the same rule the reuse logging follows.
        """
        with (
            patch("src.app.core.security.crud_token_blacklist", FakeTokenBlacklist()),
            patch("src.app.api.v1.auth.verify_token", new_callable=AsyncMock) as mock_verify,
            patch("src.app.api.v1.auth.crud_users", FakeUsers(USER_UUID, is_deleted=True)),
        ):
            mock_verify.return_value = TokenData(user_uuid=USER_UUID, session_uuid=SESSION_UUID)

            with pytest.raises(UnauthorizedException) as deleted:
                await refresh_access_token(_request({"refresh_token": "good-token"}), Mock(), mock_db)

        with patch("src.app.core.security.crud_token_blacklist", FakeTokenBlacklist()):
            with pytest.raises(UnauthorizedException) as garbage:
                await refresh_access_token(_request({"refresh_token": "not-a-jwt"}), Mock(), mock_db)

        assert deleted.value.detail == garbage.value.detail
        assert deleted.value.status_code == garbage.value.status_code


@pytest.mark.skipif(not db_available(), reason="Postgres is not reachable")
class TestRefreshLivenessAgainstPostgres:
    """The same rule against a real database, because the fake above cannot fail the way
    this would: `crud_users.exists` turns its keyword arguments into SQL, and a filter
    naming a column `User` doesn't carry raises there and nowhere else.

    Only the blacklist table is faked, so `async_db` answers exactly the one query this
    change added. Skipped when no database is reachable - `POSTGRES_SERVER=localhost` on
    a developer's machine, per CONTRIBUTING.md.
    """

    @pytest.mark.asyncio
    async def test_deleting_the_account_ends_the_sessions_it_never_saw(self, db, async_db, diver):
        """`DELETE /user` blacklists the two tokens on that request and nothing else, so
        this is the cookie on the *other* device - the stolen phone the deletion grace
        period exists for.
        """
        blacklist = FakeTokenBlacklist()

        with patch("src.app.core.security.crud_token_blacklist", blacklist):
            phone = Response()
            await issue_tokens(phone, diver.uuid, db=async_db, context=CONTEXT, user_id=diver.id)

            rotated = Response()
            await refresh_access_token(_request({"refresh_token": _refresh_cookie(phone)}), rotated, async_db)
            cookie = _refresh_cookie(rotated)

            diver.is_deleted = True
            diver.deleted_at = datetime.now(UTC).replace(tzinfo=None)
            db.commit()

            with pytest.raises(UnauthorizedException, match="Invalid refresh token."):
                await refresh_access_token(_request({"refresh_token": cookie}), Response(), async_db)

    @pytest.mark.asyncio
    async def test_a_reversed_deletion_leaves_those_sessions_working(self, db, async_db, diver):
        """The documented consequence of checking before spending the token: the account
        went dark rather than losing its sessions, so clearing the flag brings them back.
        This is what `POST /auth/restore` will rely on.
        """
        blacklist = FakeTokenBlacklist()

        with patch("src.app.core.security.crud_token_blacklist", blacklist):
            phone = Response()
            await issue_tokens(phone, diver.uuid, db=async_db, context=CONTEXT, user_id=diver.id)
            cookie = _refresh_cookie(phone)

            diver.is_deleted = True
            diver.deleted_at = datetime.now(UTC).replace(tzinfo=None)
            db.commit()

            with pytest.raises(UnauthorizedException):
                await refresh_access_token(_request({"refresh_token": cookie}), Response(), async_db)

            diver.is_deleted = False
            diver.deleted_at = None
            db.commit()

            restored = Response()
            result = await refresh_access_token(_request({"refresh_token": cookie}), restored, async_db)

            assert result["token_type"] == "bearer"
            assert _refresh_cookie(restored) != cookie

    @pytest.mark.asyncio
    async def test_a_subject_with_no_row_cannot_refresh(self, async_db, diver):
        """What a cookie minted before the purge names once the row is gone. Nothing has
        ever resolved that uuid, so this is the first thing that notices.
        """
        blacklist = FakeTokenBlacklist()

        with patch("src.app.core.security.crud_token_blacklist", blacklist):
            never_existed = Response()
            # A real `user_id`, because the session row's FK needs one - but a subject
            # uuid that resolves to nothing, which is the purged-account case. The
            # account lookup is what refuses this, before the session is ever consulted.
            await issue_tokens(never_existed, uuid_pkg.uuid4(), db=async_db, context=CONTEXT, user_id=diver.id)

            with pytest.raises(UnauthorizedException, match="Invalid refresh token."):
                await refresh_access_token(
                    _request({"refresh_token": _refresh_cookie(never_existed)}), Response(), async_db
                )
