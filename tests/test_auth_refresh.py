"""Tests for the `POST /auth/refresh` endpoint (now part of `api.v1.auth` - see
`tests/test_auth.py` for the unified email/Google/onboarding flow, and `DECISIONS.md`
for why `/refresh`/`/logout` moved under `/auth`, and for why the presented refresh
token is rotated rather than reused).

Mostly unit tests over a mocked session; the Postgres-backed classes are the trailing ones,
because the liveness check the endpoint makes is a real query and a fake cannot fail the
way it can - which is as true of the session a replayed token gets revoked as it is of the
account behind it.
"""

import logging
import uuid as uuid_pkg
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.app.api.v1.auth import _REFRESH_REPLAY_THRESHOLD, _elapsed, refresh_access_token
from src.app.core.exceptions.http_exceptions import UnauthorizedException
from src.app.core.schemas import TokenData
from src.app.core.security import blacklist_token, create_refresh_token, token_session_id
from src.app.core.utils.request_context import RequestContext
from src.app.models.user import User
from src.app.models.user_session import UserSession
from src.app.schemas.user_session import UserSessionReadInternal
from src.app.services.auth_service import issue_tokens
from tests.conftest import db_available
from tests.helpers.mocks import FakeTokenBlacklist, FrozenSecurityClock, awaited_kwargs

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


async def _spend_one_refresh_token(db: Any, *, user_uuid: uuid_pkg.UUID = USER_UUID, user_id: int = USER_ID) -> str:
    """Sign in and refresh once, returning the cookie that was spent doing so.

    Module-level rather than a method because two classes need it now: the reuse *logging*
    below, and the session revocation that the same presentation triggers past the replay
    threshold. They are two halves of one branch, and a second copy of the arrangement is
    a second thing to keep in step with `issue_tokens`.
    """
    sign_in = Response()
    await issue_tokens(sign_in, user_uuid, db=db, context=CONTEXT, user_id=user_id)
    spent = _refresh_cookie(sign_in)
    await refresh_access_token(_request({"refresh_token": spent}), Response(), db)
    return spent


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
            # (see `_handle_revoked_refresh`), so it needs somewhere to ask even when the answer
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
    nothing configures logging below it - see `_handle_revoked_refresh`) and the silence on a
    malformed token, which is noise rather than a security event.
    """

    @pytest.mark.asyncio
    async def test_reuse_logs_a_warning_naming_the_account(self, mock_db, caplog):
        blacklist = FakeTokenBlacklist()

        with (
            patch("src.app.core.security.crud_token_blacklist", blacklist),
            patch("src.app.core.security.datetime", FrozenSecurityClock),
        ):
            spent = await _spend_one_refresh_token(mock_db)

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
            spent = await _spend_one_refresh_token(mock_db)

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
            spent = await _spend_one_refresh_token(mock_db)

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


class TestAReusedRefreshTokenRevokesItsSession(SignedInAccount):
    """Rotation spends the presented cookie, so replaying *that* value fails on its own.
    What rotation cannot touch is the pair minted from it - and on a theft that pair is
    what the thief walked off with, rotating happily for the rest of its window while the
    replay the app noticed cost them nothing.

    Revoking the session both halves carry is what ends it, and the threshold is what
    keeps it off the documented two-tab race, where the only party signed out would be the
    diver themselves.

    The gap is set by backdating the blacklist row (`FakeTokenBlacklist.backdate_revocation`)
    rather than by sleeping, and **the race cases deliberately do not freeze the clock**:
    `FrozenSecurityClock` fixes the mint and the revocation together, leaving the gap to be
    whatever real time has passed since the suite imported it - which is neither side of
    the threshold reliably, and is the one thing these tests must choose.
    """

    @pytest.mark.asyncio
    async def test_a_replay_inside_the_race_window_revokes_nothing(self, mock_db):
        """The losing tab of two simultaneous refreshes lands on exactly this branch.
        Signing a diver out for it would turn a harmless collision into a real logout,
        which is the outcome recorded as the reason not to do this at all before there was
        a threshold to tell the two cases apart.
        """
        blacklist = FakeTokenBlacklist()

        with (
            patch("src.app.core.security.crud_token_blacklist", blacklist),
            patch("src.app.api.v1.auth.revoke_session", new_callable=AsyncMock) as revoke,
        ):
            spent = await _spend_one_refresh_token(mock_db)

            with pytest.raises(UnauthorizedException, match="Invalid refresh token."):
                await refresh_access_token(_request({"refresh_token": spent}), Response(), mock_db)

        revoke.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_replay_past_the_threshold_revokes_the_session_the_token_names(self, mock_db):
        """`sid` is what survives rotation, so the session the *spent* token names is the
        same one its replacement is riding - which is the whole reason revoking it reaches
        a credential the thief still holds.
        """
        blacklist = FakeTokenBlacklist()

        with (
            patch("src.app.core.security.crud_token_blacklist", blacklist),
            patch("src.app.api.v1.auth.revoke_session", new_callable=AsyncMock) as revoke,
        ):
            spent = await _spend_one_refresh_token(mock_db)
            blacklist.backdate_revocation(spent, by=_REFRESH_REPLAY_THRESHOLD + timedelta(seconds=1))

            with pytest.raises(UnauthorizedException, match="Invalid refresh token."):
                await refresh_access_token(_request({"refresh_token": spent}), Response(), mock_db)

        revoke.assert_awaited_once()
        assert awaited_kwargs(revoke)["session_uuid"] == token_session_id(spent)

    @pytest.mark.asyncio
    async def test_a_replayed_token_naming_no_session_revokes_nothing(self, mock_db):
        """A refresh token minted before sessions existed carries no `sid`, so there is
        nothing to revoke and the 401 is already the whole answer. Worth pinning because
        the alternative failure is silent: a `None` handed to `revoke_session` matches no
        row and would look exactly like this from the outside.
        """
        blacklist = FakeTokenBlacklist()

        with (
            patch("src.app.core.security.crud_token_blacklist", blacklist),
            patch("src.app.api.v1.auth.revoke_session", new_callable=AsyncMock) as revoke,
        ):
            sessionless = await create_refresh_token(data={"sub": str(USER_UUID)})
            await blacklist_token(sessionless, mock_db)
            blacklist.backdate_revocation(sessionless, by=_REFRESH_REPLAY_THRESHOLD + timedelta(seconds=1))

            with pytest.raises(UnauthorizedException, match="Invalid refresh token."):
                await refresh_access_token(_request({"refresh_token": sessionless}), Response(), mock_db)

        assert token_session_id(sessionless) is None
        revoke.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_response_still_says_nothing_about_any_of_it(self, mock_db):
        """The revocation is a side effect, not a message. A replay that ends a session and
        a cookie that was never a token answer the same 401 with the same body, or the
        endpoint becomes an oracle for which is which.
        """
        blacklist = FakeTokenBlacklist()

        with (
            patch("src.app.core.security.crud_token_blacklist", blacklist),
            patch("src.app.api.v1.auth.revoke_session", new_callable=AsyncMock),
        ):
            spent = await _spend_one_refresh_token(mock_db)
            blacklist.backdate_revocation(spent, by=_REFRESH_REPLAY_THRESHOLD + timedelta(seconds=1))

            with pytest.raises(UnauthorizedException) as replayed:
                await refresh_access_token(_request({"refresh_token": spent}), Response(), mock_db)

            with pytest.raises(UnauthorizedException) as garbage:
                await refresh_access_token(_request({"refresh_token": "not-a-jwt"}), Response(), mock_db)

        assert replayed.value.detail == garbage.value.detail
        assert replayed.value.status_code == garbage.value.status_code


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


@pytest.mark.skipif(not db_available(), reason="Postgres is not reachable")
class TestAReplayEndsTheRotatedPairAgainstPostgres:
    """What the mocked cases cannot reach: the `UPDATE` really lands, it is really
    committed before the request raises, and the cookie rotated *out* of the replayed token
    really stops working - which takes the live-session lookup running against a database
    that can fail it, rather than the permissive `FakeSessions`.

    Only the blacklist table is faked, and for one reason: the gap between a revocation and
    the presentation that follows it is the input under test, and backdating a row is the
    only way to choose it without spending real seconds.

    A theft is staged the way it happens. The first cookie is the one that leaked; the app
    rotates it for whoever presents it first, and the replay that arrives afterwards is
    what tells the server the two are not the same party.
    """

    @staticmethod
    def _live_session_count(db: Session, diver: User) -> int:
        """How many live sessions the account has **on a second connection**.

        The sync `db` session and not `async_db`, and that is the whole point of the
        helper: `revoke_session` issues its `UPDATE` inside `async_db`'s transaction, where
        an uncommitted write is visible to that same session and indistinguishable from a
        committed one. Counted there, the assertion these tests exist for would hold with
        that commit suppressed. Counted here it does not, because nothing uncommitted crosses
        between two connections.

        `expire_all()` is not the fix and was tried as one: it clears the identity map,
        which is a question about stale ORM attributes rather than about what the
        transaction can see.
        """
        return int(
            db.execute(
                select(func.count())
                .select_from(UserSession)
                .where(UserSession.user_id == diver.id, UserSession.revoked_at.is_(None))
            ).scalar_one()
        )

    @pytest.mark.asyncio
    async def test_the_pair_rotated_from_the_replayed_token_stops_working(self, async_db, diver):
        blacklist = FakeTokenBlacklist()

        with patch("src.app.core.security.crud_token_blacklist", blacklist):
            sign_in = Response()
            await issue_tokens(sign_in, diver.uuid, db=async_db, context=CONTEXT, user_id=diver.id)
            leaked = _refresh_cookie(sign_in)

            rotated = Response()
            await refresh_access_token(_request({"refresh_token": leaked}), rotated, async_db)
            replacement = _refresh_cookie(rotated)

            blacklist.backdate_revocation(leaked, by=_REFRESH_REPLAY_THRESHOLD + timedelta(seconds=1))
            with pytest.raises(UnauthorizedException, match="Invalid refresh token."):
                await refresh_access_token(_request({"refresh_token": leaked}), Response(), async_db)

            # What `async_get_db` does to a session the route raised out of, done by hand
            # because these tests call the route function rather than go through the
            # dependency. It is not tidying: it puts a real transaction boundary between
            # the replay and the presentation below, so the refusal that follows has to
            # rest on a revocation that was committed rather than on one this session can
            # still see in its own open transaction.
            await async_db.rollback()

            # The point of the whole change: the replacement was never presented, is not on
            # the blacklist, and is nowhere near its `exp` - the only thing that can refuse
            # it is the session both halves name.
            assert replacement not in blacklist.tokens
            with pytest.raises(UnauthorizedException, match="Invalid refresh token."):
                await refresh_access_token(_request({"refresh_token": replacement}), Response(), async_db)

    @pytest.mark.asyncio
    async def test_the_revocation_is_committed_and_not_left_to_the_401(self, db, async_db, diver):
        """The `UPDATE` is left for `record_auth_event` to commit, so the thing to prove is
        that a commit really happened before the endpoint raised - `async_get_db` does not
        commit on unwind, and a revocation nobody committed is a no-op that every assertion
        made on the writing session still passes.

        Which is why both counts here are taken on the sync `db` session: a second
        connection can only see what was committed. Pass that call `commit=False` - it
        takes the commit from `record_auth_event`'s default rather than an argument of its
        own - and this is the test that fails.
        """
        blacklist = FakeTokenBlacklist()

        with patch("src.app.core.security.crud_token_blacklist", blacklist):
            sign_in = Response()
            await issue_tokens(sign_in, diver.uuid, db=async_db, context=CONTEXT, user_id=diver.id)
            leaked = _refresh_cookie(sign_in)
            await refresh_access_token(_request({"refresh_token": leaked}), Response(), async_db)

            assert self._live_session_count(db, diver) == 1

            blacklist.backdate_revocation(leaked, by=_REFRESH_REPLAY_THRESHOLD + timedelta(seconds=1))
            with pytest.raises(UnauthorizedException, match="Invalid refresh token."):
                await refresh_access_token(_request({"refresh_token": leaked}), Response(), async_db)

        assert self._live_session_count(db, diver) == 0

    @pytest.mark.asyncio
    async def test_the_race_leaves_the_session_alone_and_the_replacement_working(self, db, async_db, diver):
        """The control, and the case that makes the class mean anything: a check that
        revoked on every presentation would pass both tests above. Here the second
        presentation arrives immediately - the losing tab of a simultaneous refresh - and
        the diver goes on working.
        """
        blacklist = FakeTokenBlacklist()

        with patch("src.app.core.security.crud_token_blacklist", blacklist):
            sign_in = Response()
            await issue_tokens(sign_in, diver.uuid, db=async_db, context=CONTEXT, user_id=diver.id)
            first = _refresh_cookie(sign_in)

            rotated = Response()
            await refresh_access_token(_request({"refresh_token": first}), rotated, async_db)
            replacement = _refresh_cookie(rotated)

            with pytest.raises(UnauthorizedException, match="Invalid refresh token."):
                await refresh_access_token(_request({"refresh_token": first}), Response(), async_db)

            assert self._live_session_count(db, diver) == 1

            still_working = Response()
            result = await refresh_access_token(_request({"refresh_token": replacement}), still_working, async_db)

            assert result["token_type"] == "bearer"
            assert _refresh_cookie(still_working) not in (first, replacement)
