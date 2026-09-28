"""Join links: the resolve route, `via` on the two doors that can create an account, the
gate's branches, and what the creating transaction writes.

The routing decisions - which sentence, which status, what reaches the row - are unit tests
against a mocked session. What the gate reads and what the creating transaction writes are
Postgres-backed, for the reason `test_registration_gate.py` gives: a mock evaluates no
`WHERE` and holds no transaction.

`JOIN_CHANNELS` is patched in every test that depends on it, for the reason
`test_invitations.py` patches the mode: `settings` read the developer's own `src/.env` at
import.
"""

from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
import pytest_asyncio
from fastapi import Response
from fastapi.testclient import TestClient
from jose import jwt
from pydantic import ValidationError
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api import router
from src.app.api.v1.admin import invite_batch
from src.app.api.v1.auth import (
    auth_with_google,
    complete_profile,
    request_email_link,
    verify_email_code,
    verify_email_link,
)
from src.app.api.v1.invitations import create_invitation
from src.app.api.v1.join_channels import read_join_channel
from src.app.core.config import AccountSource, RegistrationMode, settings
from src.app.core.exceptions.http_exceptions import ForbiddenException, NotFoundException
from src.app.core.schemas import GoogleUserInfo, OnboardingTokenData
from src.app.core.security import ALGORITHM, SECRET_KEY, create_onboarding_token, hash_sign_in_code
from src.app.core.setup import create_application
from src.app.models.daily_total import DailyTotal
from src.app.models.invitation import Invitation
from src.app.models.invite_request import InviteRequest
from src.app.models.user import User
from src.app.schemas.auth import (
    EmailAuthRequest,
    EmailCodeVerifyRequest,
    EmailVerifyRequest,
    GoogleAuthRequest,
    ProfileCompletionRequest,
)
from src.app.schemas.daily_total import DailyMetric
from src.app.schemas.invitation import AdminInvitationBatchRequest, InvitationCreateRequest
from src.app.services.registration_gate import (
    NOT_INVITED,
    STALE_JOIN_LINK,
    admit_or_refuse,
    refuse_uninvited,
)
from tests.conftest import db_available, unique_email, unique_username
from tests.helpers.generators import create_user
from tests.helpers.mocks import awaited_kwargs, fake_request, google_auth_body, stub_claim

CHANNELS = "scubaboard=ScubaBoard,reddit=Reddit"


def _channels(value: str | None = CHANNELS) -> Any:
    return patch.object(settings, "JOIN_CHANNELS", value)


def _mode(mode: RegistrationMode) -> Any:
    return patch.object(settings, "REGISTRATION_MODE", mode)


def _claims(token: str) -> dict[str, Any]:
    claims: dict[str, Any] = jwt.decode(token, SECRET_KEY.get_secret_value(), algorithms=[ALGORITHM])
    return claims


class TestTheResolveRoute:
    @pytest.mark.asyncio
    async def test_a_configured_slug_answers_with_its_label(self) -> None:
        with _channels():
            channel = await read_join_channel("scubaboard")

        assert channel.model_dump() == {"slug": "scubaboard", "label": "ScubaBoard"}

    @pytest.mark.asyncio
    async def test_a_slug_nobody_configured_is_a_404(self) -> None:
        with _channels(), pytest.raises(NotFoundException):
            await read_join_channel("instagram")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("unset", [None, ""])
    async def test_nothing_resolves_on_an_instance_without_channels(self, unset: str | None) -> None:
        with _channels(unset), pytest.raises(NotFoundException):
            await read_join_channel("scubaboard")


@pytest.fixture(scope="module")
def anonymous_client() -> Generator[TestClient]:
    """Its own app with no migrations on start, like `test_dive_site_catalog.py`: nothing the
    resolve route does touches a database."""
    app = create_application(router=router, settings=settings, apply_migrations_on_start=False)
    with TestClient(app) as test_client:
        yield test_client


class TestTheResolveRouteOverTheWire:
    def test_it_answers_without_a_token(self, anonymous_client: TestClient) -> None:
        with _channels():
            response = anonymous_client.get("/api/v1/join-channels/reddit")

        assert response.status_code == 200
        assert response.json() == {"slug": "reddit", "label": "Reddit"}

    @pytest.mark.parametrize("segment", ["ScubaBoard", "scuba_board", "a" * 33])
    def test_a_segment_outside_the_slug_format_is_the_same_404(
        self, anonymous_client: TestClient, segment: str
    ) -> None:
        """Not a 422: a value no operator could configure is simply not a link, and the
        route has one answer for that."""
        with _channels():
            assert anonymous_client.get(f"/api/v1/join-channels/{segment}").status_code == 404


class TestTheRequestBodies:
    @pytest.mark.parametrize("via", ["Scuba", "scuba board", "a" * 33, ""])
    def test_a_via_no_operator_could_configure_is_refused(self, via: str) -> None:
        with pytest.raises(ValidationError):
            EmailAuthRequest(email="a@example.com", via=via)
        with pytest.raises(ValidationError):
            google_auth_body(via=via)

    def test_it_is_optional_on_both(self) -> None:
        assert EmailAuthRequest(email="a@example.com").via is None
        assert google_auth_body().via is None
        assert GoogleAuthRequest.model_validate(google_auth_body(via="reddit").model_dump()).via == "reddit"


class TestTheSignInRequest:
    """`POST /auth/email/request` learns whether a link is live, and nothing about the address."""

    @staticmethod
    async def _request(mock_db: Any, *, via: str | None, mode: RegistrationMode) -> dict[str, Any]:
        with (
            _channels(),
            _mode(mode),
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock) as limited,
            patch("src.app.api.v1.auth.crud_authentication_requests") as requests,
            patch("src.app.api.v1.auth.record_auth_event", new_callable=AsyncMock) as audited,
            patch("src.app.api.v1.auth.send_magic_link_email", new_callable=AsyncMock) as sent,
        ):
            requests.count = AsyncMock(return_value=0)
            requests.create = AsyncMock(return_value=Mock(uuid=uuid7()))
            outcome: Any
            try:
                outcome = await request_email_link(
                    fake_request(), EmailAuthRequest(email="new@example.com", via=via), mock_db
                )
            except ForbiddenException as refusal:
                outcome = refusal
            return {"outcome": outcome, "limited": limited, "requests": requests, "audited": audited, "sent": sent}

    @pytest.mark.asyncio
    async def test_a_dead_link_is_refused_on_an_invite_only_instance_before_anything_is_written(self, mock_db) -> None:
        seen = await self._request(mock_db, via="instagram", mode=RegistrationMode.INVITE)

        assert isinstance(seen["outcome"], ForbiddenException)
        assert seen["outcome"].detail == STALE_JOIN_LINK
        seen["requests"].create.assert_not_called()
        seen["audited"].assert_not_awaited()
        seen["sent"].assert_not_awaited()
        seen["limited"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_dead_link_is_dropped_on_an_open_instance(self, mock_db) -> None:
        """Open mode admits the address anyway, and the refusal's way forward - the request
        form - does not exist there."""
        seen = await self._request(mock_db, via="instagram", mode=RegistrationMode.OPEN)

        assert seen["requests"].create.call_args.kwargs["object"].via is None
        seen["sent"].assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", [RegistrationMode.INVITE, RegistrationMode.OPEN])
    async def test_a_live_link_rides_the_row(self, mock_db, mode: RegistrationMode) -> None:
        seen = await self._request(mock_db, via="scubaboard", mode=mode)

        assert seen["requests"].create.call_args.kwargs["object"].via == "scubaboard"

    @pytest.mark.asyncio
    async def test_no_link_is_no_via(self, mock_db) -> None:
        seen = await self._request(mock_db, via=None, mode=RegistrationMode.INVITE)

        assert seen["requests"].create.call_args.kwargs["object"].via is None


class TestTheOnboardingToken:
    @pytest.mark.asyncio
    async def test_it_carries_the_slug_it_was_minted_with(self) -> None:
        from src.app.core.security import verify_onboarding_token

        token = await create_onboarding_token(
            OnboardingTokenData(email="a@example.com", provider="email", via="reddit")
        )

        with patch("src.app.core.security.crud_token_blacklist") as blacklist:
            blacklist.exists = AsyncMock(return_value=False)
            decoded = await verify_onboarding_token(token, Mock())

        assert decoded is not None
        assert decoded.via == "reddit"

    @pytest.mark.asyncio
    async def test_one_minted_before_the_claim_existed_reads_as_no_link(self) -> None:
        """A token the previous build minted in the deploy overlap has no `via` claim."""
        from src.app.core.security import verify_onboarding_token

        token = await create_onboarding_token(OnboardingTokenData(email="a@example.com", provider="email"))
        claims = _claims(token)
        del claims["via"]
        legacy = jwt.encode(claims, SECRET_KEY.get_secret_value(), algorithm=ALGORITHM)

        with patch("src.app.core.security.crud_token_blacklist") as blacklist:
            blacklist.exists = AsyncMock(return_value=False)
            decoded = await verify_onboarding_token(legacy, Mock())

        assert decoded is not None
        assert decoded.via is None


class TestEveryDoorHandsTheGateItsSlug:
    """The link and the code read `via` off the request row, Google off its own body; each
    hands it to the advisory gate and signs it into the onboarding token."""

    @staticmethod
    def _row(**overrides: Any) -> dict[str, Any]:
        row = {
            "id": 1,
            "uuid": uuid7(),
            "email": "new@example.com",
            "code_hash": hash_sign_in_code("481052"),
            "used_at": None,
            "invalidated_at": None,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
            "via": "scubaboard",
        }
        row.update(overrides)
        return row

    @pytest.mark.asyncio
    async def test_the_link(self, mock_db) -> None:
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as requests,
            patch("src.app.services.auth_service.crud_users") as users,
            patch("src.app.api.v1.auth.refuse_uninvited", new_callable=AsyncMock) as gate,
        ):
            requests.get = AsyncMock(return_value=self._row())
            users.get = AsyncMock(return_value=None)
            stub_claim(mock_db)

            outcome = await verify_email_link(fake_request(), EmailVerifyRequest(token="good"), Mock(), mock_db)

        assert awaited_kwargs(gate)["via"] == "scubaboard"
        assert outcome.onboarding_token is not None
        assert _claims(outcome.onboarding_token)["via"] == "scubaboard"

    @pytest.mark.asyncio
    async def test_the_code(self, mock_db) -> None:
        row = self._row()
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as requests,
            patch("src.app.services.auth_service.crud_users") as users,
            patch("src.app.api.v1.auth.refuse_uninvited", new_callable=AsyncMock) as gate,
        ):
            requests.get = AsyncMock(return_value=row)
            users.get = AsyncMock(return_value=None)
            stub_claim(mock_db)

            outcome = await verify_email_code(
                fake_request(), EmailCodeVerifyRequest(request_id=row["uuid"], code="481052"), Mock(), mock_db
            )

        assert awaited_kwargs(gate)["via"] == "scubaboard"
        assert outcome.onboarding_token is not None
        assert _claims(outcome.onboarding_token)["via"] == "scubaboard"

    @pytest.mark.asyncio
    async def test_google(self, mock_db) -> None:
        with (
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.exchange_google_code", new_callable=AsyncMock, return_value="an-id-token"),
            patch("src.app.api.v1.auth.verify_google_id_token", new_callable=AsyncMock) as verified,
            patch("src.app.services.auth_service.crud_authentication_providers") as providers,
            patch("src.app.services.auth_service.crud_users") as users,
            patch("src.app.api.v1.auth.refuse_uninvited", new_callable=AsyncMock) as gate,
        ):
            verified.return_value = GoogleUserInfo(google_id="g-1", email="New@Example.com", name="New")
            providers.get = AsyncMock(return_value=None)
            users.get = AsyncMock(return_value=None)

            outcome = await auth_with_google(fake_request(), google_auth_body(via="reddit"), Mock(), mock_db)

        assert awaited_kwargs(gate)["via"] == "reddit"
        assert outcome.onboarding_token is not None
        assert _claims(outcome.onboarding_token)["via"] == "reddit"


class TestTheAdvisoryGate:
    """`refuse_uninvited` - which sentence, and which questions it asks to reach it."""

    @pytest.mark.asyncio
    async def test_a_live_link_admits_without_asking_about_invitations(self, mock_db) -> None:
        with (
            _channels(),
            _mode(RegistrationMode.INVITE),
            patch("src.app.services.registration_gate.live_invitation_exists", new_callable=AsyncMock) as invited,
        ):
            await refuse_uninvited(mock_db, email="stranger@example.com", via="scubaboard")

        invited.assert_not_awaited()
        mock_db.scalar.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_dead_link_has_a_sentence_of_its_own(self, mock_db) -> None:
        mock_db.scalar = AsyncMock(return_value=4)
        with (
            _channels(),
            _mode(RegistrationMode.INVITE),
            patch(
                "src.app.services.registration_gate.live_invitation_exists", new_callable=AsyncMock, return_value=False
            ),
            pytest.raises(ForbiddenException) as refusal,
        ):
            await refuse_uninvited(mock_db, email="stranger@example.com", via="instagram")

        assert refusal.value.detail == STALE_JOIN_LINK

    @pytest.mark.asyncio
    async def test_a_dead_link_does_not_undo_an_invitation(self, mock_db) -> None:
        with (
            _channels(),
            _mode(RegistrationMode.INVITE),
            patch(
                "src.app.services.registration_gate.live_invitation_exists", new_callable=AsyncMock, return_value=True
            ),
        ):
            await refuse_uninvited(mock_db, email="invited@example.com", via="instagram")

    @pytest.mark.asyncio
    async def test_no_link_is_still_not_invited(self, mock_db) -> None:
        mock_db.scalar = AsyncMock(return_value=4)
        with (
            _channels(),
            _mode(RegistrationMode.INVITE),
            patch(
                "src.app.services.registration_gate.live_invitation_exists", new_callable=AsyncMock, return_value=False
            ),
            pytest.raises(ForbiddenException) as refusal,
        ):
            await refuse_uninvited(mock_db, email="stranger@example.com")

        assert refusal.value.detail == NOT_INVITED


needs_a_database = pytest.mark.skipif(not db_available(), reason="No database connection available")


@pytest_asyncio.fixture
async def sessions() -> AsyncGenerator[Any]:
    engine = create_async_engine(settings.POSTGRES_ASYNC_PREFIX + settings.POSTGRES_URI)
    yield async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


def _invite(db: Session, *, email: str, inviter: User, from_queue: bool = False) -> int:
    row = Invitation(email=email, user_id=inviter.id, uuid=uuid7(), from_invite_request=from_queue)
    db.add(row)
    db.commit()
    return row.id


@needs_a_database
class TestTheAuthoritativeGate:
    """`admit_or_refuse` against real rows: the source it names, or the sentence it refuses
    with. Each call is rolled back, which also releases the advisory lock."""

    @staticmethod
    async def _admit(sessions: Any, *, email: str, via: str | None, mode: RegistrationMode) -> str:
        async with sessions() as session:
            try:
                with _channels(), _mode(mode):
                    return await admit_or_refuse(session, email=email, via=via)
            finally:
                await session.rollback()

    @pytest.mark.asyncio
    async def test_a_live_link_admits_an_uninvited_address_under_its_slug(self, sessions: Any, diver: User) -> None:
        source = await self._admit(sessions, email=unique_email(), via="scubaboard", mode=RegistrationMode.INVITE)

        assert source == "scubaboard"

    @pytest.mark.asyncio
    async def test_the_link_wins_over_an_invitation(self, db: Session, sessions: Any, diver: User) -> None:
        """The link is what the person clicked; the invitation is still stamped accepted by
        the completion, as it always was."""
        email = unique_email()
        _invite(db, email=email, inviter=diver, from_queue=True)

        assert await self._admit(sessions, email=email, via="reddit", mode=RegistrationMode.INVITE) == "reddit"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("from_queue", "expected"), [(True, AccountSource.WAITLIST), (False, AccountSource.INVITATION)]
    )
    async def test_an_invitation_admits_under_where_it_came_from(
        self, db: Session, sessions: Any, diver: User, from_queue: bool, expected: AccountSource
    ) -> None:
        email = unique_email()
        _invite(db, email=email, inviter=diver, from_queue=from_queue)

        assert await self._admit(sessions, email=email, via=None, mode=RegistrationMode.INVITE) == expected

    @pytest.mark.asyncio
    async def test_any_queued_invitation_makes_it_the_waiting_list(
        self, db: Session, sessions: Any, diver: User
    ) -> None:
        """Two invitations for one address, the operator's among them: the operator's is
        the one that says where the person came from."""
        email = unique_email()
        _invite(db, email=email, inviter=diver)
        _invite(db, email=email, inviter=create_user(db), from_queue=True)

        source = await self._admit(sessions, email=email, via=None, mode=RegistrationMode.INVITE)

        assert source == AccountSource.WAITLIST

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("via", "expected"), [(None, "open"), ("scubaboard", "scubaboard"), ("instagram", "open")])
    async def test_an_open_instance_admits_under_the_live_slug_or_open(
        self, sessions: Any, diver: User, via: str | None, expected: str
    ) -> None:
        assert await self._admit(sessions, email=unique_email(), via=via, mode=RegistrationMode.OPEN) == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("via", "sentence"), [(None, NOT_INVITED), ("instagram", STALE_JOIN_LINK)])
    async def test_an_uninvited_address_is_refused_with_the_sentence_for_what_it_holds(
        self, sessions: Any, diver: User, via: str | None, sentence: str
    ) -> None:
        with pytest.raises(ForbiddenException) as refusal:
            await self._admit(sessions, email=unique_email(), via=via, mode=RegistrationMode.INVITE)

        assert refusal.value.detail == sentence


def _completion(email: str, *, provider: str = "email", via: str | None = None) -> OnboardingTokenData:
    return OnboardingTokenData(email=email, provider=provider, provider_user_id=None, name=None, avatar=None, via=via)


async def _complete(session: AsyncSession, token_data: OnboardingTokenData, **patches: Any) -> None:
    """`POST /auth/complete` for real against `session`, with only what happens after the
    commit - the blacklist write and the session mint - stubbed out."""
    with (
        patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
        patch("src.app.api.v1.auth.verify_onboarding_token", new_callable=AsyncMock, return_value=token_data),
        patch(
            "src.app.api.v1.auth.import_google_avatar",
            new_callable=AsyncMock,
            return_value=None,
            **({"side_effect": patches["during_avatar"]} if "during_avatar" in patches else {}),
        ),
        patch("src.app.api.v1.auth.blacklist_token", new_callable=AsyncMock),
        patch(
            "src.app.api.v1.auth.issue_tokens",
            new_callable=AsyncMock,
            return_value={"access_token": "a", "token_type": "bearer"},
        ),
    ):
        await complete_profile(
            fake_request(),
            ProfileCompletionRequest(onboarding_token="irrelevant", name="Ada Reef", username=unique_username()),
            Mock(spec=Response),
            session,
        )


def _count(db: Session, *, key: str) -> int:
    today = datetime.now(UTC).date()
    db.expire_all()
    row = db.get(DailyTotal, (today, DailyMetric.ACCOUNTS_CREATED.value, key))
    return row.count if row is not None else 0


@needs_a_database
class TestTheCreatingTransaction:
    """What `POST /auth/complete` writes beside the account, and what it refuses."""

    @pytest.mark.asyncio
    async def test_a_link_removed_before_completion_refuses_it_and_creates_nothing(
        self, db: Session, sessions: Any, diver: User
    ) -> None:
        """`test_a_revocation_committed_mid_handler_is_honoured`'s shape: the channel goes
        while the handler is between its duplicate checks and its insert, and the gate
        below the rollback reads the setting as it is by then."""
        email = unique_email()

        def remove_the_channel(_: Any) -> None:
            settings.JOIN_CHANNELS = "reddit=Reddit"

        async with sessions() as session:
            with (
                _channels(),
                _mode(RegistrationMode.INVITE),
                pytest.raises(ForbiddenException) as refusal,
            ):
                await _complete(session, _completion(email, via="scubaboard"), during_avatar=remove_the_channel)

        assert refusal.value.detail == STALE_JOIN_LINK
        assert db.query(User).filter(func.lower(User.email) == email).count() == 0

    @pytest.mark.asyncio
    async def test_a_link_counts_the_account_under_its_slug_and_clears_the_queue(
        self, db: Session, sessions: Any, diver: User
    ) -> None:
        """A capitalised Google address, because the request row is stored lowercase and
        the account's address is stored as Google sent it."""
        local = f"Beta.{uuid7().hex[-10:]}"
        email = f"{local}@Example.COM"
        db.add(InviteRequest(email=email.lower()))
        db.commit()
        before = _count(db, key="scubaboard")

        async with sessions() as session:
            with _channels(), _mode(RegistrationMode.INVITE):
                await _complete(session, _completion(email, provider="google", via="scubaboard"))

        assert _count(db, key="scubaboard") == before + 1
        assert (
            db.scalar(select(func.count()).select_from(InviteRequest).where(InviteRequest.email == email.lower())) == 0
        )
        # `EmailStr` lowercases the domain on the way in; the local part keeps its capitals.
        assert db.query(User).filter(User.email == f"{local}@example.com").count() == 1

    @pytest.mark.asyncio
    async def test_a_queued_invitation_is_counted_as_the_waiting_list_and_forgets_it(
        self, db: Session, sessions: Any, diver: User
    ) -> None:
        """Decision the model records: the flag is read by the gate and cleared by the
        acceptance in one transaction, so no accepted row - and so no account - keeps it."""
        email = unique_email()
        invitation_id = _invite(db, email=email, inviter=diver, from_queue=True)
        before = _count(db, key=AccountSource.WAITLIST)

        async with sessions() as session:
            with _channels(None), _mode(RegistrationMode.INVITE):
                await _complete(session, _completion(email))

        assert _count(db, key=AccountSource.WAITLIST) == before + 1
        db.expire_all()
        accepted = db.get(Invitation, invitation_id)
        assert accepted is not None
        assert accepted.accepted_at is not None
        assert accepted.from_invite_request is False


@needs_a_database
class TestWhereAnInvitationCameFrom:
    """`from_invite_request` is set by the operator's batch exactly when it took a request
    off the queue, and by nothing a member does."""

    @staticmethod
    def _flag(db: Session, email: str) -> list[bool]:
        db.expire_all()
        return [row.from_invite_request for row in db.query(Invitation).filter(Invitation.email == email)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("queued", [True, False])
    async def test_the_operators_batch_records_whether_it_took_one_off_the_queue(
        self, db: Session, sessions: Any, queued: bool
    ) -> None:
        operator = create_user(db, is_super_user=True)
        email = unique_email()
        if queued:
            db.add(InviteRequest(email=email))
            db.commit()

        async with sessions() as session:
            with patch("src.app.api.v1.admin.send_invitation_email", new_callable=AsyncMock):
                await invite_batch(
                    fake_request(),
                    AdminInvitationBatchRequest(emails=[email]),
                    {"id": operator.id, "name": operator.name},
                    session,
                )

        assert self._flag(db, email) == [queued]

    @pytest.mark.asyncio
    async def test_a_member_inviting_a_queued_address_records_nothing(self, db: Session, sessions: Any) -> None:
        """The member did the inviting, whatever the queue held."""
        member = create_user(db)
        email = unique_email()
        db.add(InviteRequest(email=email))
        db.commit()

        async with sessions() as session:
            with (
                _mode(RegistrationMode.INVITE),
                patch("src.app.api.v1.invitations.enforce_rate_limit", new_callable=AsyncMock),
                patch("src.app.api.v1.invitations.send_invitation_email", new_callable=AsyncMock),
            ):
                await create_invitation(
                    fake_request(),
                    InvitationCreateRequest(email=email),
                    {"id": member.id, "name": member.name, "is_superuser": False},
                    session,
                )

        assert self._flag(db, email) == [False]

    def test_rows_written_before_the_column_read_false(self, db: Session, diver: User) -> None:
        """What the outgoing build's insert gets in a deploy overlap: it names no such
        column, and the server default answers for it."""
        email = unique_email()
        db.execute(
            text("INSERT INTO invitation (email, user_id, uuid, created_at) VALUES (:email, :user_id, :uuid, now())"),
            {"email": email, "user_id": diver.id, "uuid": uuid7()},
        )
        db.commit()

        assert self._flag(db, email) == [False]
        db.execute(delete(Invitation).where(Invitation.email == email))
        db.commit()
