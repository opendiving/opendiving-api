"""The request for an invitation, the operator's batch and queue routes, and `GET /config`.

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
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1.admin import invite_batch, read_invite_requests, remove_invite_requests
from src.app.api.v1.config import read_instance_config
from src.app.api.v1.invitations import request_an_invite
from src.app.core.config import RegistrationMode, settings
from src.app.core.exceptions.http_exceptions import NotFoundException
from src.app.models.invitation import Invitation
from src.app.models.invite_request import InviteRequest
from src.app.models.user import User
from src.app.schemas.auth_audit_event import AuthEventType
from src.app.schemas.invitation import AdminInvitationBatchRequest, InvitationReadInternal
from src.app.schemas.invite_request import AdminInviteRequestDeleteRequest, InviteRequestSubmission
from src.app.services import map_renderer
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
CREDIT = "[OpenFreeMap](https://openfreemap.org/) © [OpenMapTiles](https://openmaptiles.org/)"


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
    """The request route answers 404: there is nothing to ask an open instance's operator
    for, and a 403 would say the feature exists and the caller may not use it."""

    @pytest.mark.asyncio
    async def test_the_anonymous_request_route(self, mock_db) -> None:
        with _mode(RegistrationMode.OPEN), pytest.raises(NotFoundException):
            await request_an_invite(_request(), InviteRequestSubmission(email="a@example.com"), mock_db)

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
        absence of any read rather than by the sameness of the response, because the
        response being the same is what a shaping bug looks like from outside: with the
        write and the audit row mocked, the handler touches the session only to commit.
        """
        with (
            _mode(RegistrationMode.INVITE),
            patch("src.app.api.v1.invitations.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.invitations.record_invite_request", new_callable=AsyncMock),
            patch("src.app.api.v1.invitations.record_auth_event", new_callable=AsyncMock),
        ):
            await request_an_invite(_request(), InviteRequestSubmission(email="a@example.com"), mock_db)

        for read in (mock_db.execute, mock_db.scalar, mock_db.scalars, mock_db.get):
            read.assert_not_called()

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
        # Per-IP first: a request it refuses must not have spent the address's bucket.
        assert keys == ["invite-request:ip:203.0.113.7", "invite-request:email:a@example.com"]


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
            patch("src.app.api.v1.admin.delete_invite_requests", new_callable=AsyncMock, return_value=0),
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
            patch("src.app.api.v1.admin.delete_invite_requests", new_callable=AsyncMock, return_value=0),
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
            patch("src.app.api.v1.admin.delete_invite_requests", new_callable=AsyncMock, return_value=0),
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
            patch("src.app.api.v1.admin.delete_invite_requests", new_callable=AsyncMock, return_value=0),
            patch("src.app.api.v1.admin.record_auth_event", new_callable=AsyncMock),
            patch("src.app.api.v1.admin.send_invitation_email", new_callable=AsyncMock),
        ):
            crud.create = AsyncMock(return_value=_internal())

            response = await invite_batch(
                _request(), AdminInvitationBatchRequest(emails=["fresh@example.com"]), OPERATOR, mock_db
            )

        assert [r.outcome for r in response.results] == ["invited"]

    @pytest.mark.asyncio
    async def test_the_request_row_and_the_audit_row_ride_the_insert_s_transaction(self, mock_db) -> None:
        """Invariant: an address with a live invitation has no request row. One commit, so
        the two tables cannot be observed disagreeing."""
        with (
            patch("src.app.api.v1.admin.account_exists_for", new_callable=AsyncMock, return_value=False),
            patch("src.app.api.v1.admin.live_invitation_from", new_callable=AsyncMock, return_value=False),
            patch("src.app.api.v1.admin.crud_invitations") as crud,
            patch("src.app.api.v1.admin.delete_invite_requests", new_callable=AsyncMock, return_value=1) as cleared,
            patch("src.app.api.v1.admin.record_auth_event", new_callable=AsyncMock) as recorder,
            patch("src.app.api.v1.admin.send_invitation_email", new_callable=AsyncMock),
        ):
            crud.create = AsyncMock(return_value=_internal())

            await invite_batch(
                _request(), AdminInvitationBatchRequest(emails=["Friend@Example.com"]), OPERATOR, mock_db
            )

        assert crud.create.await_args.kwargs["commit"] is False
        assert crud.create.await_args.kwargs["object"].email == "friend@example.com"
        assert crud.create.await_args.kwargs["object"].from_invite_request is True
        assert awaited_kwargs(cleared)["commit"] is False
        assert awaited_kwargs(cleared)["emails"] == ["friend@example.com"]
        written = awaited_kwargs(recorder)
        assert written["event_type"] is AuthEventType.INVITATION_CREATED
        assert written["user_id"] == OPERATOR["id"]
        assert written["email"] == "friend@example.com"
        assert written["commit"] is False
        mock_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_email_is_sent_after_the_commit_and_names_the_operator(self, mock_db) -> None:
        """After, deliberately: a send that fails leaves a real invitation rather than
        rolling one back, because the address is admitted from the commit onwards."""
        order: list[str] = []
        mock_db.commit = AsyncMock(side_effect=lambda: order.append("commit"))
        with (
            patch("src.app.api.v1.admin.account_exists_for", new_callable=AsyncMock, return_value=False),
            patch("src.app.api.v1.admin.live_invitation_from", new_callable=AsyncMock, return_value=False),
            patch("src.app.api.v1.admin.crud_invitations") as crud,
            patch("src.app.api.v1.admin.delete_invite_requests", new_callable=AsyncMock, return_value=0),
            patch("src.app.api.v1.admin.record_auth_event", new_callable=AsyncMock),
            patch(
                "src.app.api.v1.admin.send_invitation_email",
                new_callable=AsyncMock,
                side_effect=lambda **_: order.append("mail"),
            ) as mail,
        ):
            crud.create = AsyncMock(return_value=_internal())

            await invite_batch(
                _request(), AdminInvitationBatchRequest(emails=["friend@example.com"]), OPERATOR, mock_db
            )

        assert order == ["commit", "mail"]
        assert awaited_kwargs(mail) == {"email": "friend@example.com", "inviter_name": OPERATOR["name"]}

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
    @pytest.mark.parametrize(("channels", "expected"), [(None, False), ("", False), ("scubaboard=ScubaBoard", True)])
    async def test_it_reports_whether_any_join_link_exists(self, channels: str | None, expected: bool) -> None:
        """A yes or no, never the list - that is resolved one slug at a time."""
        with patch.object(settings, "JOIN_CHANNELS", channels):
            assert (await read_instance_config()).join_links is expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("renderer", "expected"), [("", False), ("http://map-renderer:3001", True)])
    async def test_it_reports_whether_map_tiles_are_drawn(self, renderer: str, expected: bool) -> None:
        with patch.object(settings, "MAP_RENDERER_URL", renderer):
            assert (await read_instance_config()).map_tiles is expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("renderer", "learned", "expected"),
        [
            pytest.param("http://map-renderer:3001", CREDIT, CREDIT, id="the renderer's credit"),
            pytest.param("http://map-renderer:3001", None, None, id="none learned yet"),
            pytest.param("", CREDIT, None, id="no tiles, whatever was learned"),
        ],
    )
    async def test_it_reports_the_credit_the_renderer_named(
        self, renderer: str, learned: str | None, expected: str | None
    ) -> None:
        with (
            patch.object(settings, "MAP_RENDERER_URL", renderer),
            patch.object(map_renderer, "_attribution", learned),
        ):
            assert (await read_instance_config()).map_attribution == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enabled", [False, True])
    async def test_it_reports_whether_the_mcp_server_runs(self, enabled: bool) -> None:
        with patch.object(settings, "MCP_ENABLED", enabled):
            assert (await read_instance_config()).mcp_enabled is enabled

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("features", "expected"),
        [
            (None, []),
            ("", []),
            ("all", ["all"]),
            ("trips-v2, all,dive-planner,trips-v2", ["all", "dive-planner", "trips-v2"]),
        ],
    )
    async def test_it_reports_the_experimental_features(self, features: str | None, expected: list[str]) -> None:
        """Repeated as the setting holds them, each once and sorted: no registry of names exists
        to expand `all` from, and an empty list rather than a missing member is what a client
        reads as none."""
        with patch.object(settings, "EXPERIMENTAL_FEATURES", features):
            assert (await read_instance_config()).experimental_features == expected

    @pytest.mark.asyncio
    async def test_it_carries_nothing_else(self) -> None:
        """Adding a field is a decision rather than a convenience: this endpoint is anonymous,
        so everything on it is public. Dumped in JSON mode because this is the wire shape the
        web app is written against, name for name."""
        with (
            _mode(RegistrationMode.INVITE),
            _operated(False),
            patch.object(settings, "COMMONS_PRIMARY", False),
            patch.object(settings, "JOIN_CHANNELS", None),
            patch.object(settings, "MAP_RENDERER_URL", ""),
            patch.object(settings, "DIVE_CONDITIONS_PROVIDER", ""),
            patch.object(settings, "MCP_ENABLED", False),
            patch.object(settings, "EXPERIMENTAL_FEATURES", None),
        ):
            assert (await read_instance_config()).model_dump(mode="json") == {
                "registration_mode": "invite",
                "project_operated": False,
                "commons_primary": False,
                "join_links": False,
                "map_tiles": False,
                "map_attribution": None,
                "dive_conditions": False,
                "mcp_enabled": False,
                "experimental_features": [],
            }


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestAgainstPostgres:
    """The claims a mocked session answers wrongly rather than not at all.

    Chiefly the case-folded account comparison: `POST /auth/complete` inserts the onboarding
    token's address verbatim, so a Google-born account's `User.email` may carry capitals
    while both invitation tables store lowercase - and a mock returns whatever it was told
    to whichever query is issued.
    """

    @pytest.mark.asyncio
    async def test_an_account_stored_with_capitals_is_still_found(
        self, db: Session, async_sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """The bug invariant 7 exists to prevent: a lowercased invitation address compared
        against the raw column misses exactly the Google-born accounts, and the inviter is
        then told an address is free when it is not."""
        from src.app.crud.crud_invitations import account_exists_for

        shouty = create_user(db)
        shouty.email = f"Mixed.Case.{shouty.id}@Example.COM"
        db.commit()

        async with async_sessions() as session:
            assert await account_exists_for(session, email=shouty.email.lower()) is True

    @pytest.mark.asyncio
    async def test_the_quota_lock_is_per_inviter_and_held_to_the_transactions_end(
        self, db: Session, diver: User, async_sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        from sqlalchemy import func

        from src.app.crud.crud_invitations import _INVITATION_QUOTA_LOCK, hold_invitation_quota

        other = create_user(db)

        def try_lock(user_id: int) -> Any:
            return select(func.pg_try_advisory_xact_lock(_INVITATION_QUOTA_LOCK, user_id))

        async with async_sessions() as one, async_sessions() as two:
            await hold_invitation_quota(one, user_id=diver.id)

            assert await two.scalar(try_lock(diver.id)) is False
            assert await two.scalar(try_lock(other.id)) is True
            await two.rollback()

            await one.commit()
            assert await two.scalar(try_lock(diver.id)) is True
            await two.rollback()

    @pytest.mark.asyncio
    async def test_the_quota_counts_revoked_rows_inside_the_window(
        self, db: Session, diver: User, async_sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """Counting rows rather than live invitations is what makes the quota a bound on
        invitations made: revoking one after the send does not unsend it."""
        from src.app.crud.crud_invitations import invitations_created_since

        rows = [
            Invitation(email=unique_email(), user_id=diver.id, uuid=uuid7()),
            Invitation(email=unique_email(), user_id=diver.id, uuid=uuid7(), revoked_at=datetime.now(UTC)),
            Invitation(email=unique_email(), user_id=diver.id, uuid=uuid7(), accepted_at=datetime.now(UTC)),
        ]
        for row in rows:
            db.add(row)
        db.commit()

        async with async_sessions() as session:
            assert await invitations_created_since(session, user_id=diver.id, window_days=1) == 3

    @pytest.mark.asyncio
    async def test_a_row_outside_the_window_is_not_counted(
        self, db: Session, diver: User, async_sessions: async_sessionmaker[AsyncSession]
    ) -> None:
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

        async with async_sessions() as session:
            assert await invitations_created_since(session, user_id=diver.id, window_days=1) == 0

    @pytest.mark.asyncio
    async def test_the_crud_helpers_commit_only_when_asked(
        self, db: Session, diver: User, async_sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """Both helpers take `commit` because both are called two ways: standalone from an
        operator route, and `commit=False` from inside a transaction somebody else ends.
        Getting that backwards is how "one commit" quietly becomes two.
        """
        from src.app.crud.crud_invitations import accept_invitations
        from src.app.crud.crud_invite_requests import delete_invite_requests

        email = unique_email()
        try:
            async with async_sessions() as session:
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
            async with async_sessions() as cleanup:
                await cleanup.execute(delete(Invitation).where(Invitation.email == email))
                await cleanup.execute(delete(InviteRequest).where(InviteRequest.email == email))
                await cleanup.commit()

    @pytest.mark.asyncio
    async def test_a_repeat_request_stores_one_row(
        self, db: Session, async_sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """`ON CONFLICT DO NOTHING` rather than a check followed by an insert: two
        submissions of one address have no window in which both find nothing, and the
        endpoint has one answer rather than a branch that could be got wrong."""
        from src.app.crud.crud_invite_requests import record_invite_request

        email = unique_email()
        try:
            async with async_sessions() as session:
                await record_invite_request(session, email=email)
                await record_invite_request(session, email=email)

                rows = (await session.execute(select(InviteRequest).where(InviteRequest.email == email))).all()
                assert len(rows) == 1
        finally:
            async with async_sessions() as cleanup:
                await cleanup.execute(delete(InviteRequest).where(InviteRequest.email == email))
                await cleanup.commit()

    @pytest.mark.asyncio
    async def test_the_queue_flags_an_address_that_already_has_an_account(
        self, db: Session, async_sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """`has_account` on a `lower(User.email)` comparison - *this* route may look, and it
        has to look case-insensitively for the same reason the 409 does."""
        shouty = create_user(db)
        shouty.email = f"Queued.{shouty.id}@Example.COM"
        db.commit()
        queued = shouty.email.lower()
        stranger = unique_email()

        try:
            async with async_sessions() as session:
                session.add_all([InviteRequest(email=queued), InviteRequest(email=stranger)])
                await session.commit()

                page = await read_invite_requests(session, page=1, items_per_page=100)

            flags = {row["email"]: row["has_account"] for row in page["data"]}
            assert flags[queued] is True
            assert flags[stranger] is False
        finally:
            async with async_sessions() as cleanup:
                await cleanup.execute(delete(InviteRequest).where(InviteRequest.email.in_([queued, stranger])))
                await cleanup.commit()

    @pytest.mark.asyncio
    async def test_inviting_an_address_clears_its_request_in_one_transaction(
        self, db: Session, diver: User, async_sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """The invariant stated end to end rather than by asserting `commit=False` on two
        mocks: after the route returns, the address has an invitation and no request row."""
        email = unique_email()

        try:
            async with async_sessions() as session:
                session.add(InviteRequest(email=email))
                await session.commit()

                operator = {"id": diver.id, "uuid": diver.uuid, "name": diver.name, "is_superuser": True}
                with patch("src.app.api.v1.admin.send_invitation_email", new_callable=AsyncMock):
                    await invite_batch(
                        _request(), AdminInvitationBatchRequest(emails=[email.upper()]), operator, session
                    )

                requests = (await session.execute(select(InviteRequest).where(InviteRequest.email == email))).all()
                invitations = (await session.execute(select(Invitation).where(Invitation.email == email))).all()

            assert requests == []
            assert len(invitations) == 1
        finally:
            async with async_sessions() as cleanup:
                await cleanup.execute(delete(Invitation).where(Invitation.email == email))
                await cleanup.execute(delete(InviteRequest).where(InviteRequest.email == email))
                await cleanup.commit()
