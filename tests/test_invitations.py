"""The invitation routes, the request queue, the operator's three, and `GET /config`.

Mostly unit tests calling the handlers directly with a mocked session: what these routes do
is branch on mode, count, compare and choose a status code, and every one of those is a
decision in Python. The exceptions are gathered in the Postgres-backed classes at the
bottom, and they are the claims a mock would answer wrongly rather than not at all - the
case-folded account comparison, the quota's window, and the two tables agreeing inside one
transaction.

The mode is patched in every test that depends on it. It has a default (`invite`) and
`settings` is read at import, so a test that left it alone would pass or fail on whatever
the developer's own `src/.env` happens to say - the same trap `test_support.py`'s
`_configured_inbox` fixture exists for.
"""

import uuid as uuid_pkg
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1.admin import invite_batch, read_invite_requests, remove_invite_requests
from src.app.api.v1.config import read_instance_config
from src.app.api.v1.invitations import (
    create_invitation,
    read_invitations,
    request_an_invite,
    revoke_invitation,
)
from src.app.core.config import RegistrationMode, settings
from src.app.core.exceptions.http_exceptions import NotFoundException, RateLimitException
from src.app.models.invitation import Invitation
from src.app.models.invite_request import InviteRequest
from src.app.models.user import User
from src.app.schemas.auth_audit_event import AuthEventType
from src.app.schemas.invitation import (
    AdminInvitationBatchRequest,
    InvitationCreateRequest,
    InvitationReadInternal,
)
from src.app.schemas.invite_request import AdminInviteRequestDeleteRequest, InviteRequestSubmission
from tests.conftest import db_available, unique_email
from tests.helpers.generators import create_user
from tests.helpers.mocks import awaited_kwargs


def _mode(mode: RegistrationMode) -> Any:
    return patch.object(settings, "REGISTRATION_MODE", mode)


def _operated(value: bool) -> Any:
    return patch.object(settings, "PROJECT_OPERATED", value)


def _request() -> Mock:
    request = Mock()
    request.client = Mock(host="203.0.113.7")
    request.headers = {}
    return request


@pytest.fixture(autouse=True)
def _no_live_rate_limiter():
    """Every handler in this module consults `enforce_rate_limit`, and none of these tests
    is about Redis.

    Autouse rather than repeated per test because the failure mode is asymmetric and
    misleading: the compose stack does **not** publish Redis to the host, so on a developer's
    machine the limiter fails open (`DECISIONS.md` §"Rate limiting fails open on a Redis
    *outage*") and an unpatched test passes. CI runs Redis as a service container on
    localhost, where the real limiter runs instead - so the same test can be green locally
    and red in CI, which is the one direction the Postgres skip trap does not cover.

    The tests that are genuinely *about* the limiter re-patch it themselves; an inner
    `patch` wins over this one.
    """
    with patch("src.app.api.v1.invitations.enforce_rate_limit", new_callable=AsyncMock):
        yield


def awaited_args(recorder: Any) -> tuple:
    """The positional arguments of a mock's last await, narrowed for mypy.

    `AsyncMock.await_args` is typed `_Call | None`, so reading `.args` off it does not
    type-check - the sibling of `tests.helpers.mocks.awaited_kwargs`, which does the same
    for the keyword half.
    """
    calls = recorder.await_args_list
    assert calls, "the mock was never awaited"
    return tuple(calls[-1].args)


CALLER = {"id": 7, "uuid": uuid_pkg.uuid4(), "name": "Ada Reef", "is_superuser": False}
OPERATOR = {**CALLER, "id": 9, "is_superuser": True}


def _internal(**overrides: Any) -> InvitationReadInternal:
    base = {
        "id": 1,
        "uuid": uuid7(),
        "user_id": CALLER["id"],
        "email": "friend@example.com",
        "created_at": datetime.now(UTC),
        "accepted_at": None,
        "revoked_at": None,
    }
    return InvitationReadInternal(**{**base, **overrides})


class TestOpenModeMakesTheFeatureAbsent:
    """Every route answers 404, which is what lets the web's settings card remove itself
    with no knowledge of the mode - the self-hiding the sessions and passkeys cards already
    do. A 403 would tell the card the feature exists and it is not allowed to use it, which
    is a different and wrong sentence."""

    @pytest.mark.asyncio
    async def test_the_anonymous_request_route(self, mock_db) -> None:
        with _mode(RegistrationMode.OPEN), pytest.raises(NotFoundException):
            await request_an_invite(_request(), InviteRequestSubmission(email="a@example.com"), mock_db)

    @pytest.mark.asyncio
    async def test_the_list_route(self, mock_db) -> None:
        with _mode(RegistrationMode.OPEN), pytest.raises(NotFoundException):
            await read_invitations(CALLER, mock_db)

    @pytest.mark.asyncio
    async def test_the_create_route(self, mock_db) -> None:
        with _mode(RegistrationMode.OPEN), pytest.raises(NotFoundException):
            await create_invitation(_request(), InvitationCreateRequest(email="a@example.com"), CALLER, mock_db)

    @pytest.mark.asyncio
    async def test_the_revoke_route(self, mock_db) -> None:
        with _mode(RegistrationMode.OPEN), pytest.raises(NotFoundException):
            await revoke_invitation(uuid7(), CALLER, mock_db)

    @pytest.mark.asyncio
    async def test_nothing_is_written_or_rate_limited_on_the_way_out(self, mock_db) -> None:
        """The mode check is above the rate limiters, the way `send_support_request` puts
        its 503 above its own: an instance where the feature is off must not have its
        buckets spent by traffic that was never going to be stored."""
        with (
            _mode(RegistrationMode.OPEN),
            patch("src.app.api.v1.invitations.enforce_rate_limit", new_callable=AsyncMock) as limiter,
            patch("src.app.api.v1.invitations.record_invite_request", new_callable=AsyncMock) as stored,
            pytest.raises(NotFoundException),
        ):
            await request_an_invite(_request(), InviteRequestSubmission(email="a@example.com"), mock_db)

        limiter.assert_not_awaited()
        stored.assert_not_awaited()


class TestRequestAnInvite:
    @pytest.mark.asyncio
    async def test_the_address_is_lowercased_on_the_way_in(self, mock_db) -> None:
        with (
            _mode(RegistrationMode.INVITE),
            patch("src.app.api.v1.invitations.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.invitations.record_invite_request", new_callable=AsyncMock) as stored,
            patch("src.app.api.v1.invitations.record_auth_event", new_callable=AsyncMock),
        ):
            await request_an_invite(_request(), InviteRequestSubmission(email="Beta.Tester@Example.COM"), mock_db)

        assert awaited_kwargs(stored)["email"] == "beta.tester@example.com"

    @pytest.mark.asyncio
    async def test_it_never_queries_the_user_table(self, mock_db) -> None:
        """The structural guarantee, and the reason this endpoint may exist anonymously at
        all: an answer that varied with whether an address has an account would be an
        enumeration oracle for anybody who can reach the port. Asserted the way
        `test_never_queries_whether_the_user_exists` asserts it one endpoint over - by the
        absence of the call rather than by the sameness of the response, because the
        response being the same is what a shaping bug looks like from outside.
        """
        with (
            _mode(RegistrationMode.INVITE),
            patch("src.app.api.v1.invitations.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.invitations.record_invite_request", new_callable=AsyncMock),
            patch("src.app.api.v1.invitations.record_auth_event", new_callable=AsyncMock),
            patch("src.app.api.v1.invitations.account_exists_for", new_callable=AsyncMock) as looked,
        ):
            await request_an_invite(_request(), InviteRequestSubmission(email="a@example.com"), mock_db)

        looked.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_message_is_the_same_object_every_time(self, mock_db) -> None:
        """A frozen module-level response rather than one composed per call: there is no
        branch that could make it differ, which is stronger than every branch happening to
        produce the same words."""
        with (
            _mode(RegistrationMode.INVITE),
            patch("src.app.api.v1.invitations.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.invitations.record_invite_request", new_callable=AsyncMock),
            patch("src.app.api.v1.invitations.record_auth_event", new_callable=AsyncMock),
        ):
            first = await request_an_invite(_request(), InviteRequestSubmission(email="a@example.com"), mock_db)
            second = await request_an_invite(_request(), InviteRequestSubmission(email="b@example.com"), mock_db)

        assert first is second

    @pytest.mark.asyncio
    async def test_the_audit_row_is_written_user_less_and_unconditionally(self, mock_db) -> None:
        """Emitted on every accepted request including one whose on-conflict insert was a
        no-op, because what the row carries beyond the address is the IP and User-Agent -
        the same reason `AUTH_REQUEST_CREATED` is unconditional."""
        with (
            _mode(RegistrationMode.INVITE),
            patch("src.app.api.v1.invitations.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.invitations.record_invite_request", new_callable=AsyncMock),
            patch("src.app.api.v1.invitations.record_auth_event", new_callable=AsyncMock) as recorder,
        ):
            await request_an_invite(_request(), InviteRequestSubmission(email="A@Example.com"), mock_db)

        written = awaited_kwargs(recorder)
        assert written["event_type"] is AuthEventType.INVITE_REQUESTED
        assert "user_id" not in written or written["user_id"] is None
        assert written["email"] == "a@example.com"

    @pytest.mark.asyncio
    async def test_both_limiters_are_spent_before_anything_is_stored(self, mock_db) -> None:
        with (
            _mode(RegistrationMode.INVITE),
            patch("src.app.api.v1.invitations.enforce_rate_limit", new_callable=AsyncMock) as limiter,
            patch("src.app.api.v1.invitations.record_invite_request", new_callable=AsyncMock),
            patch("src.app.api.v1.invitations.record_auth_event", new_callable=AsyncMock),
        ):
            await request_an_invite(_request(), InviteRequestSubmission(email="a@example.com"), mock_db)

        keys = [call.args[0] for call in limiter.await_args_list]
        assert keys == ["invite-request:email:a@example.com", "invite-request:ip:203.0.113.7"]


class TestReadInvitations:
    """`GET /user/invitations` - the contract the web client's settings card is built against."""

    @pytest.mark.asyncio
    async def test_it_pages_the_callers_own_rows_newest_first(self, mock_db) -> None:
        row = {
            "id": 1,
            "uuid": uuid7(),
            "user_id": CALLER["id"],
            "email": "friend@example.com",
            "created_at": datetime.now(UTC),
            "accepted_at": None,
            "revoked_at": None,
        }

        with (
            _mode(RegistrationMode.INVITE),
            patch("src.app.api.v1.invitations.crud_invitations") as crud,
        ):
            crud.get_multi = AsyncMock(return_value={"data": [row], "total_count": 1})

            page = await read_invitations(CALLER, mock_db, page=1, items_per_page=10)

        asked = crud.get_multi.await_args.kwargs
        assert asked["user_id"] == CALLER["id"]
        assert asked["sort_columns"] == ["created_at"]
        assert asked["sort_orders"] == ["desc"]
        assert page["total_count"] == 1
        assert page["data"] == [
            {
                "uuid": row["uuid"],
                "email": "friend@example.com",
                "created_at": row["created_at"],
                "accepted_at": None,
                "revoked_at": None,
            }
        ]

    @pytest.mark.asyncio
    async def test_no_internal_id_reaches_the_response(self, mock_db) -> None:
        """The rows come back carrying `id` and `user_id` because `schema_to_select` is the
        crud alias's own select schema. The handler builds the public shape rather than
        leaving `response_model` to strip them - a route that reused this helper without one
        would otherwise put a sequential id on the wire."""
        row = {
            "id": 4242,
            "uuid": uuid7(),
            "user_id": CALLER["id"],
            "email": "friend@example.com",
            "created_at": datetime.now(UTC),
            "accepted_at": None,
            "revoked_at": None,
        }

        with _mode(RegistrationMode.INVITE), patch("src.app.api.v1.invitations.crud_invitations") as crud:
            crud.get_multi = AsyncMock(return_value={"data": [row], "total_count": 1})

            page = await read_invitations(CALLER, mock_db)

        assert "id" not in page["data"][0]
        assert "user_id" not in page["data"][0]

    @pytest.mark.asyncio
    async def test_the_pagination_is_clamped(self, mock_db) -> None:
        """`clamp_pagination`, like every list route: these arrive off the query string, so
        `?items_per_page=999999999` is a request for the caller's whole table."""
        with _mode(RegistrationMode.INVITE), patch("src.app.api.v1.invitations.crud_invitations") as crud:
            crud.get_multi = AsyncMock(return_value={"data": [], "total_count": 0})

            page = await read_invitations(CALLER, mock_db, page=-3, items_per_page=999_999)

        assert crud.get_multi.await_args.kwargs["limit"] == 100
        assert page["page"] == 1
        assert page["items_per_page"] == 100


class TestTheProbeThrottle:
    """`POST /user/invitations` answers a distinguishable 409 for a registered address, and
    that refusal creates nothing - so the quota, counted from rows created, never charges
    for it.

    Without a throttle above the check, a signed-in caller can walk a wordlist through this
    endpoint and learn who is registered, without bound, and can keep telling the two
    answers apart even after their quota is spent (409 for a member, 429 for everyone else).
    `PATCH /user` guards the identical shape for its username-availability check.
    """

    @staticmethod
    def _quiet_dependencies() -> tuple:
        return (
            patch("src.app.api.v1.invitations.account_exists_for", new_callable=AsyncMock, return_value=True),
            patch("src.app.api.v1.invitations.live_invitation_from", new_callable=AsyncMock, return_value=False),
            patch("src.app.api.v1.invitations.invitations_created_since", new_callable=AsyncMock, return_value=0),
        )

    @pytest.mark.asyncio
    async def test_the_throttle_runs_before_the_existence_check(self, mock_db) -> None:
        """Above it, not beside it. A limiter consulted after the lookup would still let the
        oracle answer on the request that trips it, and - more to the point - would leave the
        ordering free to drift back."""
        account, already, quota = self._quiet_dependencies()
        with (
            _mode(RegistrationMode.INVITE),
            patch(
                "src.app.api.v1.invitations.enforce_rate_limit",
                new_callable=AsyncMock,
                side_effect=RateLimitException("slow down"),
            ),
            account as looked,
            already,
            quota,
            pytest.raises(RateLimitException),
        ):
            await create_invitation(_request(), InvitationCreateRequest(email="member@example.com"), CALLER, mock_db)

        looked.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_refused_probe_is_still_charged(self, mock_db) -> None:
        """The whole point: the 409 path creates no invitation row, so the quota cannot see
        it - the throttle is what a probe spends."""
        account, already, quota = self._quiet_dependencies()
        with (
            _mode(RegistrationMode.INVITE),
            patch("src.app.api.v1.invitations.enforce_rate_limit", new_callable=AsyncMock) as limiter,
            account,
            already,
            quota,
            pytest.raises(HTTPException) as refusal,
        ):
            await create_invitation(_request(), InvitationCreateRequest(email="member@example.com"), CALLER, mock_db)

        assert refusal.value.status_code == 409
        limiter.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_it_is_keyed_per_user_and_not_per_ip(self, mock_db) -> None:
        """The caller is authenticated, so there is a better key than their address - and a
        per-IP bucket would let one office share one probing budget."""
        account, already, quota = self._quiet_dependencies()
        with (
            _mode(RegistrationMode.INVITE),
            patch("src.app.api.v1.invitations.enforce_rate_limit", new_callable=AsyncMock) as limiter,
            account,
            already,
            quota,
            pytest.raises(HTTPException),
        ):
            await create_invitation(_request(), InvitationCreateRequest(email="member@example.com"), CALLER, mock_db)

        key, limit, _window = awaited_args(limiter)
        assert key == f"invitation-create:user:{CALLER['id']}"
        assert limit == settings.INVITATION_ATTEMPT_RATE_LIMIT_PER_USER

    @pytest.mark.asyncio
    async def test_a_superuser_is_throttled_too(self, mock_db) -> None:
        """Exempt from the quota, which bounds how many people they may invite; not from the
        backstop against automated probing, which is a different question."""
        account, already, quota = self._quiet_dependencies()
        with (
            _mode(RegistrationMode.INVITE),
            patch("src.app.api.v1.invitations.enforce_rate_limit", new_callable=AsyncMock) as limiter,
            account,
            already,
            quota,
            pytest.raises(HTTPException),
        ):
            await create_invitation(_request(), InvitationCreateRequest(email="member@example.com"), OPERATOR, mock_db)

        limiter.assert_awaited_once()
        assert awaited_args(limiter)[0] == f"invitation-create:user:{OPERATOR['id']}"


class TestCreateInvitation:
    """The five refusals, each a distinct status so the card can show the message verbatim."""

    @staticmethod
    def _patches(**overrides: Any) -> Any:
        defaults: dict[str, Any] = {"account": False, "already": False, "sent": 0}
        defaults.update(overrides)
        return (
            patch(
                "src.app.api.v1.invitations.account_exists_for",
                new_callable=AsyncMock,
                return_value=defaults["account"],
            ),
            patch(
                "src.app.api.v1.invitations.live_invitation_from",
                new_callable=AsyncMock,
                return_value=defaults["already"],
            ),
            patch(
                "src.app.api.v1.invitations.invitations_created_since",
                new_callable=AsyncMock,
                return_value=defaults["sent"],
            ),
        )

    @pytest.mark.asyncio
    async def test_an_address_with_an_account_is_a_409(self, mock_db) -> None:
        account, already, quota = self._patches(account=True)
        with _mode(RegistrationMode.INVITE), account, already, quota, pytest.raises(HTTPException) as refusal:
            await create_invitation(_request(), InvitationCreateRequest(email="member@example.com"), CALLER, mock_db)

        assert refusal.value.status_code == 409
        assert "already has an account" in refusal.value.detail

    @pytest.mark.asyncio
    async def test_a_second_live_invitation_from_the_same_inviter_is_a_409(self, mock_db) -> None:
        account, already, quota = self._patches(already=True)
        with _mode(RegistrationMode.INVITE), account, already, quota, pytest.raises(HTTPException) as refusal:
            await create_invitation(_request(), InvitationCreateRequest(email="friend@example.com"), CALLER, mock_db)

        assert refusal.value.status_code == 409
        assert "already invited" in refusal.value.detail

    @pytest.mark.asyncio
    async def test_the_quota_is_a_429_naming_the_limit(self, mock_db) -> None:
        account, already, quota = self._patches(sent=5)
        with (
            _mode(RegistrationMode.INVITE),
            patch.object(settings, "INVITATIONS_PER_USER", 5),
            patch.object(settings, "INVITATIONS_WINDOW_DAYS", 1),
            account,
            already,
            quota,
            pytest.raises(HTTPException) as refusal,
        ):
            await create_invitation(_request(), InvitationCreateRequest(email="friend@example.com"), CALLER, mock_db)

        assert refusal.value.status_code == 429
        assert "5 invitations per day" in refusal.value.detail

    @pytest.mark.asyncio
    async def test_a_multi_day_window_reads_as_a_window(self, mock_db) -> None:
        """ "per 3 days" rather than "per day": the sentence is shown verbatim to a diver,
        and the singular phrasing is only correct for the default."""
        account, already, quota = self._patches(sent=2)
        with (
            _mode(RegistrationMode.INVITE),
            patch.object(settings, "INVITATIONS_PER_USER", 2),
            patch.object(settings, "INVITATIONS_WINDOW_DAYS", 3),
            account,
            already,
            quota,
            pytest.raises(HTTPException) as refusal,
        ):
            await create_invitation(_request(), InvitationCreateRequest(email="friend@example.com"), CALLER, mock_db)

        assert "2 invitations every 3 days" in refusal.value.detail

    @pytest.mark.asyncio
    async def test_a_superuser_is_exempt_from_the_quota(self, mock_db) -> None:
        """And the exemption is structural: the count is never even taken, so a superuser
        cannot be refused by a stale one."""
        account, already, quota = self._patches(sent=999)
        with (
            _mode(RegistrationMode.INVITE),
            account,
            already,
            quota as counted,
            patch("src.app.api.v1.invitations.crud_invitations") as crud,
            patch("src.app.api.v1.invitations.delete_invite_requests", new_callable=AsyncMock),
            patch("src.app.api.v1.invitations.record_auth_event", new_callable=AsyncMock),
            patch("src.app.api.v1.invitations.send_invitation_email", new_callable=AsyncMock),
        ):
            crud.create = AsyncMock(return_value=_internal())

            await create_invitation(_request(), InvitationCreateRequest(email="friend@example.com"), OPERATOR, mock_db)

        counted.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_request_row_and_the_audit_row_ride_the_insert_s_transaction(self, mock_db) -> None:
        """Invariant: an address with a live invitation has no request row. One commit, so
        the two tables cannot be observed disagreeing."""
        account, already, quota = self._patches()
        with (
            _mode(RegistrationMode.INVITE),
            account,
            already,
            quota,
            patch("src.app.api.v1.invitations.crud_invitations") as crud,
            patch("src.app.api.v1.invitations.delete_invite_requests", new_callable=AsyncMock) as cleared,
            patch("src.app.api.v1.invitations.record_auth_event", new_callable=AsyncMock) as recorder,
            patch("src.app.api.v1.invitations.send_invitation_email", new_callable=AsyncMock),
        ):
            crud.create = AsyncMock(return_value=_internal())

            await create_invitation(_request(), InvitationCreateRequest(email="Friend@Example.com"), CALLER, mock_db)

        assert crud.create.await_args.kwargs["commit"] is False
        assert crud.create.await_args.kwargs["object"].email == "friend@example.com"
        assert awaited_kwargs(cleared)["commit"] is False
        assert awaited_kwargs(cleared)["emails"] == ["friend@example.com"]
        written = awaited_kwargs(recorder)
        assert written["event_type"] is AuthEventType.INVITATION_CREATED
        assert written["user_id"] == CALLER["id"]
        assert written["email"] == "friend@example.com"
        assert written["commit"] is False
        mock_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_email_is_sent_after_the_commit_and_names_the_inviter(self, mock_db) -> None:
        """After, deliberately: a send that fails leaves a real invitation rather than
        rolling one back, because the address is admitted from the commit onwards and the
        inviter can tell them another way."""
        account, already, quota = self._patches()
        with (
            _mode(RegistrationMode.INVITE),
            account,
            already,
            quota,
            patch("src.app.api.v1.invitations.crud_invitations") as crud,
            patch("src.app.api.v1.invitations.delete_invite_requests", new_callable=AsyncMock),
            patch("src.app.api.v1.invitations.record_auth_event", new_callable=AsyncMock),
            patch("src.app.api.v1.invitations.send_invitation_email", new_callable=AsyncMock) as mail,
        ):
            crud.create = AsyncMock(return_value=_internal())

            await create_invitation(_request(), InvitationCreateRequest(email="friend@example.com"), CALLER, mock_db)

        sent = awaited_kwargs(mail)
        assert sent == {"email": "friend@example.com", "inviter_name": "Ada Reef"}


class TestRevokeInvitation:
    @pytest.mark.asyncio
    async def test_an_accepted_invitation_cannot_be_revoked(self, mock_db) -> None:
        """409 rather than a silent success: the account exists, so the stamp would assert
        something untrue, and it would take away nothing - the address is admitted by having
        an account, not by the invitation."""
        with (
            _mode(RegistrationMode.INVITE),
            patch(
                "src.app.api.v1.invitations.fetch_owned_or_raise",
                new_callable=AsyncMock,
                return_value=_internal(accepted_at=datetime.now(UTC)),
            ),
            pytest.raises(HTTPException) as refusal,
        ):
            await revoke_invitation(uuid7(), CALLER, mock_db)

        assert refusal.value.status_code == 409
        mock_db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_second_revoke_succeeds_and_writes_nothing(self, mock_db) -> None:
        """The `UserSession` shape rather than the hard-deleting resources': the row is not
        what is being removed, and the caller already owns it."""
        already_revoked = _internal(revoked_at=datetime.now(UTC))
        with (
            _mode(RegistrationMode.INVITE),
            patch(
                "src.app.api.v1.invitations.fetch_owned_or_raise", new_callable=AsyncMock, return_value=already_revoked
            ),
        ):
            result = await revoke_invitation(uuid7(), CALLER, mock_db)

        assert result.message == "Invitation revoked"
        mock_db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_pending_invitation_is_stamped(self, mock_db) -> None:
        with (
            _mode(RegistrationMode.INVITE),
            patch("src.app.api.v1.invitations.fetch_owned_or_raise", new_callable=AsyncMock, return_value=_internal()),
        ):
            await revoke_invitation(uuid7(), CALLER, mock_db)

        mock_db.execute.assert_awaited_once()
        mock_db.commit.assert_awaited_once()


class TestTheOperatorsBatch:
    @pytest.mark.asyncio
    async def test_each_address_gets_its_own_outcome(self, mock_db) -> None:
        """A per-address report rather than a 5xx on the first problem, so a partial failure
        is visible as *which* addresses got through."""
        with (
            patch(
                "src.app.api.v1.admin.account_exists_for",
                new_callable=AsyncMock,
                side_effect=[True, False, False],
            ),
            patch("src.app.api.v1.admin.live_invitation_from", new_callable=AsyncMock, side_effect=[True, False]),
            patch("src.app.api.v1.admin.crud_invitations") as crud,
            patch("src.app.api.v1.admin.delete_invite_requests", new_callable=AsyncMock),
            patch("src.app.api.v1.admin.record_auth_event", new_callable=AsyncMock),
            patch("src.app.api.v1.admin.send_invitation_email", new_callable=AsyncMock),
        ):
            crud.create = AsyncMock(return_value=_internal())

            response = await invite_batch(
                _request(),
                AdminInvitationBatchRequest(emails=["member@example.com", "invited@example.com", "fresh@example.com"]),
                OPERATOR,
                mock_db,
            )

        assert [(r.email, r.outcome) for r in response.results] == [
            ("member@example.com", "already_registered"),
            ("invited@example.com", "already_invited"),
            ("fresh@example.com", "invited"),
        ]

    @pytest.mark.asyncio
    async def test_a_failed_send_is_reported_and_the_invitation_stands(self, mock_db) -> None:
        """The outcome that matters most: the row is committed by then and the address is
        admitted, so the operator's job is to tell them another way rather than to invite
        again."""
        with (
            patch("src.app.api.v1.admin.account_exists_for", new_callable=AsyncMock, return_value=False),
            patch("src.app.api.v1.admin.live_invitation_from", new_callable=AsyncMock, return_value=False),
            patch("src.app.api.v1.admin.crud_invitations") as crud,
            patch("src.app.api.v1.admin.delete_invite_requests", new_callable=AsyncMock),
            patch("src.app.api.v1.admin.record_auth_event", new_callable=AsyncMock),
            patch(
                "src.app.api.v1.admin.send_invitation_email",
                new_callable=AsyncMock,
                side_effect=RuntimeError("relay refused"),
            ),
        ):
            crud.create = AsyncMock(return_value=_internal())

            response = await invite_batch(
                _request(), AdminInvitationBatchRequest(emails=["fresh@example.com"]), OPERATOR, mock_db
            )

        assert [r.outcome for r in response.results] == ["mail_failed"]
        mock_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_one_relay_failure_does_not_take_the_batch_with_it(self, mock_db) -> None:
        with (
            patch("src.app.api.v1.admin.account_exists_for", new_callable=AsyncMock, return_value=False),
            patch("src.app.api.v1.admin.live_invitation_from", new_callable=AsyncMock, return_value=False),
            patch("src.app.api.v1.admin.crud_invitations") as crud,
            patch("src.app.api.v1.admin.delete_invite_requests", new_callable=AsyncMock),
            patch("src.app.api.v1.admin.record_auth_event", new_callable=AsyncMock),
            patch(
                "src.app.api.v1.admin.send_invitation_email",
                new_callable=AsyncMock,
                side_effect=[RuntimeError("relay refused"), None],
            ),
        ):
            crud.create = AsyncMock(return_value=_internal())

            response = await invite_batch(
                _request(),
                AdminInvitationBatchRequest(emails=["one@example.com", "two@example.com"]),
                OPERATOR,
                mock_db,
            )

        assert [r.outcome for r in response.results] == ["mail_failed", "invited"]

    @pytest.mark.asyncio
    async def test_the_batch_ignores_the_registration_mode(self, mock_db) -> None:
        """The operator's routes are not the member's: an instance can be flipped to `open`
        while a queue still holds requests somebody has to answer, and there is no reason
        that should 404."""
        with (
            _mode(RegistrationMode.OPEN),
            patch("src.app.api.v1.admin.account_exists_for", new_callable=AsyncMock, return_value=False),
            patch("src.app.api.v1.admin.live_invitation_from", new_callable=AsyncMock, return_value=False),
            patch("src.app.api.v1.admin.crud_invitations") as crud,
            patch("src.app.api.v1.admin.delete_invite_requests", new_callable=AsyncMock),
            patch("src.app.api.v1.admin.record_auth_event", new_callable=AsyncMock),
            patch("src.app.api.v1.admin.send_invitation_email", new_callable=AsyncMock),
        ):
            crud.create = AsyncMock(return_value=_internal())

            response = await invite_batch(
                _request(), AdminInvitationBatchRequest(emails=["fresh@example.com"]), OPERATOR, mock_db
            )

        assert [r.outcome for r in response.results] == ["invited"]

    @pytest.mark.asyncio
    async def test_removal_lowercases_and_reports_what_actually_went(self, mock_db) -> None:
        with patch("src.app.api.v1.admin.delete_invite_requests", new_callable=AsyncMock, return_value=1) as removed:
            response = await remove_invite_requests(
                AdminInviteRequestDeleteRequest(emails=["Spam@Example.COM", "gone@example.com"]), mock_db
            )

        assert awaited_kwargs(removed)["emails"] == ["spam@example.com", "gone@example.com"]
        assert response.removed == 1

    def test_the_batch_is_bounded(self) -> None:
        """`MAX_ADDRESSES_PER_BATCH`, because the sends are inline and sequential - an
        unbounded body would be an unbounded request."""
        from pydantic import ValidationError

        from src.app.schemas.invitation import MAX_ADDRESSES_PER_BATCH

        with pytest.raises(ValidationError):
            AdminInvitationBatchRequest(emails=[f"a{n}@example.com" for n in range(MAX_ADDRESSES_PER_BATCH + 1)])

        with pytest.raises(ValidationError):
            AdminInvitationBatchRequest(emails=[])


class TestTheConfigRoute:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", [RegistrationMode.OPEN, RegistrationMode.INVITE])
    async def test_it_reports_the_mode(self, mode: RegistrationMode) -> None:
        with _mode(mode):
            assert (await read_instance_config()).registration_mode is mode

    @pytest.mark.asyncio
    @pytest.mark.parametrize("operated", [False, True])
    async def test_it_reports_whether_the_project_operates_the_instance(self, operated: bool) -> None:
        """Patched like the mode, and for the same reason: `settings` read the developer's own
        `src/.env` at import, so the live value proves nothing about the code. The declared
        default is asserted in `test_config_safety.py`, off a `config.py` loaded with no
        `.env` in reach."""
        with _operated(operated):
            assert (await read_instance_config()).project_operated is operated

    @pytest.mark.asyncio
    async def test_it_carries_nothing_else(self) -> None:
        """Two fields, and adding another is a decision rather than a convenience: this
        endpoint is anonymous, so everything on it is public. Dumped in JSON mode because
        this is the wire shape the web app is written against, name for name."""
        with _mode(RegistrationMode.INVITE), _operated(False):
            assert (await read_instance_config()).model_dump(mode="json") == {
                "registration_mode": "invite",
                "project_operated": False,
            }


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestAgainstPostgres:
    """The claims a mocked session answers wrongly rather than not at all.

    Chiefly the case-folded account comparison: `POST /auth/complete` inserts the onboarding
    token's address verbatim, so a Google-born account's `User.email` may carry capitals
    while both invitation tables store lowercase - and a mock returns whatever it was told
    to whichever query is issued.
    """

    @pytest_asyncio.fixture
    async def sessions(self) -> AsyncGenerator[Any]:
        engine = create_async_engine(settings.POSTGRES_ASYNC_PREFIX + settings.POSTGRES_URI)
        yield async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_an_account_stored_with_capitals_is_still_found(self, db: Session, sessions: Any) -> None:
        """The bug invariant 7 exists to prevent: a lowercased invitation address compared
        against the raw column misses exactly the Google-born accounts, and the inviter is
        then told an address is free when it is not."""
        from src.app.crud.crud_invitations import account_exists_for

        shouty = create_user(db)
        shouty.email = f"Mixed.Case.{shouty.id}@Example.COM"
        db.commit()

        async with sessions() as session:
            assert await account_exists_for(session, email=shouty.email.lower()) is True

    @pytest.mark.asyncio
    async def test_the_quota_counts_revoked_rows_inside_the_window(self, db: Session, diver: User) -> None:
        """Counting rows rather than live invitations is what makes the quota a bound on
        emails sent: revoking one after the send does not unsend it."""
        from src.app.crud.crud_invitations import invitations_created_since

        rows = [
            Invitation(email=unique_email(), user_id=diver.id, uuid=uuid7()),
            Invitation(email=unique_email(), user_id=diver.id, uuid=uuid7(), revoked_at=datetime.now(UTC)),
            Invitation(email=unique_email(), user_id=diver.id, uuid=uuid7(), accepted_at=datetime.now(UTC)),
        ]
        for row in rows:
            db.add(row)
        db.commit()

        engine = create_async_engine(settings.POSTGRES_ASYNC_PREFIX + settings.POSTGRES_URI)
        try:
            async with async_sessionmaker(bind=engine, class_=AsyncSession)() as session:
                assert await invitations_created_since(session, user_id=diver.id, window_days=1) == 3
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_a_row_outside_the_window_is_not_counted(self, db: Session, diver: User) -> None:
        """A rate, not a lifetime allotment - which is the whole difference between this and
        Lemmy's `max_invites_per_user_allowed`."""
        from src.app.crud.crud_invitations import invitations_created_since

        old = Invitation(
            email=unique_email(),
            user_id=diver.id,
            uuid=uuid7(),
            created_at=datetime.now(UTC) - timedelta(days=2),
        )
        db.add(old)
        db.commit()

        engine = create_async_engine(settings.POSTGRES_ASYNC_PREFIX + settings.POSTGRES_URI)
        try:
            async with async_sessionmaker(bind=engine, class_=AsyncSession)() as session:
                assert await invitations_created_since(session, user_id=diver.id, window_days=1) == 0
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_the_crud_helpers_commit_only_when_asked(self, db: Session, diver: User, sessions: Any) -> None:
        """Both helpers take `commit` because both are called two ways: standalone from an
        operator route, and `commit=False` from inside a transaction somebody else ends.
        Getting that backwards is how "one commit" quietly becomes two.
        """
        from src.app.crud.crud_invitations import accept_invitations
        from src.app.crud.crud_invite_requests import delete_invite_requests

        email = unique_email()
        try:
            async with sessions() as session:
                session.add(Invitation(email=email, user_id=diver.id, uuid=uuid7()))
                session.add(InviteRequest(email=email))
                await session.commit()

                assert await accept_invitations(session, email=email, commit=True) == 1
                assert await delete_invite_requests(session, emails=[email], commit=True) == 1
                # Nothing to match is not an error on either, which is the ordinary case:
                # a bootstrap account has no invitation, and an operator may invite an
                # address that never asked.
                assert await accept_invitations(session, email=unique_email(), commit=True) == 0
                assert await delete_invite_requests(session, emails=[], commit=True) == 0
        finally:
            async with sessions() as cleanup:
                await cleanup.execute(delete(Invitation).where(Invitation.email == email))
                await cleanup.execute(delete(InviteRequest).where(InviteRequest.email == email))
                await cleanup.commit()

    @pytest.mark.asyncio
    async def test_a_repeat_request_stores_one_row(self, db: Session, sessions: Any) -> None:
        """`ON CONFLICT DO NOTHING` rather than a check followed by an insert: two
        submissions of one address have no window in which both find nothing, and the
        endpoint has one answer rather than a branch that could be got wrong."""
        from src.app.crud.crud_invite_requests import record_invite_request

        email = unique_email()
        try:
            async with sessions() as session:
                await record_invite_request(session, email=email)
                await record_invite_request(session, email=email)

                rows = (await session.execute(select(InviteRequest).where(InviteRequest.email == email))).all()
                assert len(rows) == 1
        finally:
            async with sessions() as cleanup:
                await cleanup.execute(delete(InviteRequest).where(InviteRequest.email == email))
                await cleanup.commit()

    @pytest.mark.asyncio
    async def test_the_queue_flags_an_address_that_already_has_an_account(self, db: Session, sessions: Any) -> None:
        """`has_account` on a `lower(User.email)` comparison - *this* route may look, and it
        has to look case-insensitively for the same reason the 409 does."""
        shouty = create_user(db)
        shouty.email = f"Queued.{shouty.id}@Example.COM"
        db.commit()
        queued = shouty.email.lower()
        stranger = unique_email()

        try:
            async with sessions() as session:
                session.add_all([InviteRequest(email=queued), InviteRequest(email=stranger)])
                await session.commit()

                page = await read_invite_requests(session, page=1, items_per_page=100)

            flags = {row["email"]: row["has_account"] for row in page["data"]}
            assert flags[queued] is True
            assert flags[stranger] is False
        finally:
            async with sessions() as cleanup:
                await cleanup.execute(delete(InviteRequest).where(InviteRequest.email.in_([queued, stranger])))
                await cleanup.commit()

    @pytest.mark.asyncio
    async def test_inviting_an_address_clears_its_request_in_one_transaction(
        self, db: Session, diver: User, sessions: Any
    ) -> None:
        """The invariant stated end to end rather than by asserting `commit=False` on two
        mocks: after the route returns, the address has an invitation and no request row."""
        email = unique_email()

        try:
            async with sessions() as session:
                session.add(InviteRequest(email=email))
                await session.commit()

                caller = {"id": diver.id, "uuid": diver.uuid, "name": diver.name, "is_superuser": False}
                with (
                    _mode(RegistrationMode.INVITE),
                    patch("src.app.api.v1.invitations.send_invitation_email", new_callable=AsyncMock),
                ):
                    await create_invitation(_request(), InvitationCreateRequest(email=email.upper()), caller, session)

                requests = (await session.execute(select(InviteRequest).where(InviteRequest.email == email))).all()
                invitations = (await session.execute(select(Invitation).where(Invitation.email == email))).all()

            assert requests == []
            assert len(invitations) == 1
        finally:
            async with sessions() as cleanup:
                await cleanup.execute(delete(Invitation).where(Invitation.email == email))
                await cleanup.execute(delete(InviteRequest).where(InviteRequest.email == email))
                await cleanup.commit()
