"""A member's invite link: the token, the member's three routes, the resolver, the sign-in
request, the gate's link branches, what the creating transaction writes, and the row the
inviter reads back.

The routing decisions - which status, which sentence, which questions get asked - are unit
tests against a mocked session. What the gate reads, what the upsert does under a race and
what the creating transaction writes are Postgres-backed, for the reason
`test_registration_gate.py` gives: a mock evaluates no `WHERE` and holds no transaction.

The mode and `JOIN_CHANNELS` are patched wherever they matter, since `settings` read the
developer's own `src/.env` at import.
"""

import asyncio
import re
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
import pytest_asyncio
from fastapi import Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1.auth import complete_profile, request_email_link
from src.app.api.v1.invitations import (
    erase_invite_link,
    make_invite_link,
    read_invitations,
    read_own_invite_link,
    resolve_invite_link,
)
from src.app.core.config import JOIN_CHANNEL_SLUG_PATTERN, AccountSource, RegistrationMode, settings
from src.app.core.exceptions.http_exceptions import ForbiddenException, NotFoundException
from src.app.core.schemas import OnboardingTokenData
from src.app.crud.crud_invitations import invitations_created_since
from src.app.models.daily_total import DailyTotal
from src.app.models.invitation import Invitation
from src.app.models.invite_link import InviteLink
from src.app.models.invite_request import InviteRequest
from src.app.models.user import User
from src.app.schemas.auth import EmailAuthRequest, ProfileCompletionRequest
from src.app.schemas.auth_audit_event import AuthEventType
from src.app.schemas.daily_total import DailyMetric
from src.app.services.invite_links import LiveInviteLink, invite_link_exists, new_invite_link_token
from src.app.services.registration_gate import (
    NOT_INVITED,
    SPENT_INVITE_LINK,
    STALE_INVITE_LINK,
    STALE_JOIN_LINK,
    admit_or_refuse,
    refuse_uninvited,
)
from tests.conftest import db_available, unique_email, unique_username
from tests.helpers.generators import create_user
from tests.helpers.mocks import awaited_kwargs, fake_request

CALLER = {"id": 7, "name": "Ada Reef", "is_superuser": False}
LIVE = LiveInviteLink(owner_id=7, owner_name="Ada Reef", owner_is_superuser=False)


def _mode(mode: RegistrationMode) -> Any:
    return patch.object(settings, "REGISTRATION_MODE", mode)


def _channels(value: str | None = "scubaboard=ScubaBoard") -> Any:
    return patch.object(settings, "JOIN_CHANNELS", value)


class TestTheToken:
    def test_it_fits_every_seat_a_join_link_slug_takes(self) -> None:
        """`via`'s pattern on both request bodies, the request row's `String(32)` and the web's
        copy of the pattern on the Google attempt record: the token rides all three."""
        token = new_invite_link_token()

        assert len(token) == 32
        assert re.fullmatch(JOIN_CHANNEL_SLUG_PATTERN, token)

    def test_each_one_is_fresh(self) -> None:
        assert len({new_invite_link_token() for _ in range(100)}) == 100


class TestOpenModeMakesTheLinkAbsent:
    """As every invitation route does: 404, so the card's link block never learns the mode."""

    @pytest.mark.asyncio
    async def test_reading_it(self, mock_db) -> None:
        with _mode(RegistrationMode.OPEN), pytest.raises(NotFoundException):
            await read_own_invite_link(CALLER, mock_db)

    @pytest.mark.asyncio
    async def test_making_it(self, mock_db) -> None:
        with _mode(RegistrationMode.OPEN), pytest.raises(NotFoundException):
            await make_invite_link(fake_request(), CALLER, mock_db)

    @pytest.mark.asyncio
    async def test_removing_it(self, mock_db) -> None:
        with _mode(RegistrationMode.OPEN), pytest.raises(NotFoundException):
            await erase_invite_link(fake_request(), CALLER, mock_db)

    @pytest.mark.asyncio
    async def test_resolving_it_is_the_same_uncacheable_404_and_reads_nothing(self, mock_db) -> None:
        with (
            _mode(RegistrationMode.OPEN),
            patch("src.app.api.v1.invitations.live_invite_link", new_callable=AsyncMock) as looked_up,
        ):
            answer = await resolve_invite_link("a" * 32, Response(), mock_db)

        assert isinstance(answer, JSONResponse)
        assert answer.status_code == 404
        assert answer.headers["Cache-Control"] == "private, no-store"
        looked_up.assert_not_awaited()


class TestTheResolver:
    @pytest.mark.asyncio
    async def test_a_live_link_answers_with_its_owners_name_and_is_not_cacheable(self, mock_db) -> None:
        response = Response()
        with (
            _mode(RegistrationMode.INVITE),
            patch("src.app.api.v1.invitations.live_invite_link", new_callable=AsyncMock, return_value=LIVE),
        ):
            answer = await resolve_invite_link("a" * 32, response, mock_db)

        assert answer.model_dump() == {"inviter_name": "Ada Reef"}  # type: ignore[union-attr]
        assert response.headers["Cache-Control"] == "private, no-store"

    @pytest.mark.asyncio
    async def test_anything_else_is_one_uncacheable_404(self, mock_db) -> None:
        with (
            _mode(RegistrationMode.INVITE),
            patch("src.app.api.v1.invitations.live_invite_link", new_callable=AsyncMock, return_value=None),
        ):
            answer = await resolve_invite_link("not-a-token", Response(), mock_db)

        assert isinstance(answer, JSONResponse)
        assert answer.status_code == 404
        assert answer.headers["Cache-Control"] == "private, no-store"


class TestTheMembersRoutesAudit:
    @pytest.mark.asyncio
    async def test_making_or_resetting_it_is_one_event_on_the_members_account(self, mock_db) -> None:
        mock_db.commit = AsyncMock()
        with (
            _mode(RegistrationMode.INVITE),
            patch(
                "src.app.api.v1.invitations.replace_invite_link",
                new_callable=AsyncMock,
                return_value=("a" * 32, datetime.now(UTC)),
            ),
            patch("src.app.api.v1.invitations.record_auth_event", new_callable=AsyncMock) as recorder,
        ):
            made = await make_invite_link(fake_request(), CALLER, mock_db)

        assert made.token == "a" * 32
        written = awaited_kwargs(recorder)
        assert written["event_type"] is AuthEventType.INVITE_LINK_CREATED
        assert written["user_id"] == CALLER["id"]
        assert "email" not in written

    @pytest.mark.asyncio
    @pytest.mark.parametrize("there_was_one", [True, False])
    async def test_removing_it_is_an_event_only_when_there_was_one(self, mock_db, there_was_one: bool) -> None:
        """Criterion (c) is a credential revoked; with nothing there, nothing was."""
        mock_db.commit = AsyncMock()
        with (
            _mode(RegistrationMode.INVITE),
            patch("src.app.api.v1.invitations.remove_invite_link", new_callable=AsyncMock, return_value=there_was_one),
            patch("src.app.api.v1.invitations.record_auth_event", new_callable=AsyncMock) as recorder,
        ):
            answer = await erase_invite_link(fake_request(), CALLER, mock_db)

        assert answer.message == "Invite link removed"
        if there_was_one:
            assert awaited_kwargs(recorder)["event_type"] is AuthEventType.INVITE_LINK_REMOVED
        else:
            recorder.assert_not_awaited()


class TestTheSignInRequest:
    """`POST /auth/email/request` asks whether a token names an `invite_link` row, and
    nothing about any account."""

    @staticmethod
    async def _request(mock_db: Any, *, via: str, mode: RegistrationMode, known: bool, channels: str | None) -> Any:
        with (
            _channels(channels),
            _mode(mode),
            patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.crud_authentication_requests") as requests,
            patch("src.app.api.v1.auth.record_auth_event", new_callable=AsyncMock),
            patch("src.app.api.v1.auth.send_magic_link_email", new_callable=AsyncMock),
            patch(
                "src.app.services.registration_gate.invite_link_exists", new_callable=AsyncMock, return_value=known
            ) as exists,
            patch("src.app.services.registration_gate.live_invite_link", new_callable=AsyncMock) as live,
        ):
            requests.count = AsyncMock(return_value=0)
            requests.create = AsyncMock(return_value=Mock(uuid=uuid7()))
            try:
                await request_email_link(fake_request(), EmailAuthRequest(email="new@example.com", via=via), mock_db)
            except ForbiddenException as refusal:
                return {"refusal": refusal, "requests": requests, "exists": exists, "live": live}
            return {"refusal": None, "requests": requests, "exists": exists, "live": live}

    @pytest.mark.asyncio
    async def test_a_members_link_rides_the_row(self, mock_db) -> None:
        token = new_invite_link_token()
        seen = await self._request(mock_db, via=token, mode=RegistrationMode.INVITE, known=True, channels=None)

        assert seen["refusal"] is None
        assert seen["requests"].create.call_args.kwargs["object"].via == token
        # The owner's deletion state and quota are the gates' question, after the address is proven.
        seen["live"].assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("channels", "sentence"), [(None, STALE_INVITE_LINK), ("reddit=Reddit", STALE_JOIN_LINK)])
    async def test_a_dead_token_is_refused_with_the_sentence_for_what_the_instance_has(
        self, mock_db, channels: str | None, sentence: str
    ) -> None:
        """With no channel configured, a dead `via` can only have been a member's link, which
        is never called a join link."""
        seen = await self._request(
            mock_db, via=new_invite_link_token(), mode=RegistrationMode.INVITE, known=False, channels=channels
        )

        assert seen["refusal"].detail == sentence
        seen["requests"].create.assert_not_called()

    @pytest.mark.asyncio
    async def test_open_mode_drops_the_token_without_looking_it_up(self, mock_db) -> None:
        seen = await self._request(
            mock_db, via=new_invite_link_token(), mode=RegistrationMode.OPEN, known=True, channels=None
        )

        assert seen["requests"].create.call_args.kwargs["object"].via is None
        seen["exists"].assert_not_awaited()


class TestTheAdvisoryGate:
    """`refuse_uninvited`'s link branches, with the reads mocked."""

    @staticmethod
    async def _ask(mock_db: Any, *, link: LiveInviteLink | None, can_invite: bool, invited: bool) -> Any:
        mock_db.scalar = AsyncMock(return_value=4)
        with (
            _channels(None),
            _mode(RegistrationMode.INVITE),
            patch("src.app.services.registration_gate.live_invite_link", new_callable=AsyncMock, return_value=link),
            patch(
                "src.app.services.registration_gate.can_still_invite", new_callable=AsyncMock, return_value=can_invite
            ),
            patch(
                "src.app.services.registration_gate.live_invitation_exists",
                new_callable=AsyncMock,
                return_value=invited,
            ) as asked,
        ):
            try:
                await refuse_uninvited(mock_db, email="stranger@example.com", via="a" * 32)
            except ForbiddenException as refusal:
                return refusal.detail, asked
            return None, asked

    @pytest.mark.asyncio
    async def test_a_link_under_quota_admits_without_asking_about_invitations(self, mock_db) -> None:
        refused, asked = await self._ask(mock_db, link=LIVE, can_invite=True, invited=False)

        assert refused is None
        asked.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_spent_link_refuses_an_uninvited_address_with_a_sentence_of_its_own(self, mock_db) -> None:
        refused, _ = await self._ask(mock_db, link=LIVE, can_invite=False, invited=False)

        assert refused == SPENT_INVITE_LINK

    @pytest.mark.asyncio
    async def test_a_spent_link_steps_aside_for_an_invitation(self, mock_db) -> None:
        refused, _ = await self._ask(mock_db, link=LIVE, can_invite=False, invited=True)

        assert refused is None

    @pytest.mark.asyncio
    async def test_a_dead_token_is_stale(self, mock_db) -> None:
        refused, _ = await self._ask(mock_db, link=None, can_invite=False, invited=False)

        assert refused == STALE_INVITE_LINK


needs_a_database = pytest.mark.skipif(not db_available(), reason="No database connection available")


@pytest_asyncio.fixture
async def sessions() -> AsyncGenerator[Any]:
    engine = create_async_engine(settings.POSTGRES_ASYNC_PREFIX + settings.POSTGRES_URI)
    yield async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


def _caller(user: User) -> dict[str, Any]:
    return {"id": user.id, "name": user.name, "is_superuser": user.is_superuser}


def _link(db: Session, owner: User) -> str:
    token = new_invite_link_token()
    db.add(InviteLink(user_id=owner.id, token=token))
    db.commit()
    return token


def _invite(db: Session, *, email: str, inviter: User, from_queue: bool = False) -> int:
    row = Invitation(email=email, user_id=inviter.id, from_invite_request=from_queue)
    db.add(row)
    db.commit()
    return row.id


@needs_a_database
class TestTheMembersRoutes:
    @pytest.mark.asyncio
    async def test_make_read_reset_and_remove(self, db: Session, sessions: Any) -> None:
        member = create_user(db)
        caller = _caller(member)

        async with sessions() as session:
            with _mode(RegistrationMode.INVITE):
                with pytest.raises(NotFoundException):
                    await read_own_invite_link(caller, session)

                first = await make_invite_link(fake_request(), caller, session)
                assert (await read_own_invite_link(caller, session)).token == first.token
                assert (await resolve_invite_link(first.token, Response(), session)).model_dump() == {  # type: ignore[union-attr]
                    "inviter_name": member.name
                }

                second = await make_invite_link(fake_request(), caller, session)
                assert second.token != first.token
                assert second.created_at >= first.created_at
                # A reset stops the old token resolving at once.
                stale = await resolve_invite_link(first.token, Response(), session)
                assert isinstance(stale, JSONResponse)
                assert stale.status_code == 404

                await erase_invite_link(fake_request(), caller, session)
                with pytest.raises(NotFoundException):
                    await read_own_invite_link(caller, session)
                gone = await resolve_invite_link(second.token, Response(), session)
                assert isinstance(gone, JSONResponse)

        db.expire_all()
        assert db.scalar(select(func.count()).select_from(InviteLink).where(InviteLink.user_id == member.id)) == 0

    @pytest.mark.asyncio
    async def test_two_first_creates_racing_both_succeed_and_leave_one_row(self, db: Session, sessions: Any) -> None:
        """The upsert on `user_id`: a double click or two tabs never meet the unique index."""
        member = create_user(db)
        caller = _caller(member)

        async def make() -> str:
            async with sessions() as session:
                return (await make_invite_link(fake_request(), caller, session)).token

        with _mode(RegistrationMode.INVITE):
            tokens = await asyncio.gather(make(), make())

        db.expire_all()
        stored = db.scalars(select(InviteLink.token).where(InviteLink.user_id == member.id)).all()
        assert len(stored) == 1
        assert stored[0] in tokens

    @pytest.mark.asyncio
    async def test_the_sign_in_requests_question_is_whether_the_row_exists_and_nothing_else(
        self, db: Session, sessions: Any
    ) -> None:
        """A deleted owner's token still exists here: whether the owner is deleted is the
        gates' question, after the address is proven, and not the anonymous endpoint's."""
        owner = create_user(db)
        token = _link(db, owner)
        owner.is_deleted = True
        owner.deleted_at = datetime.now(UTC)
        db.commit()

        async with sessions() as session:
            assert await invite_link_exists(session, token=token) is True
            assert await invite_link_exists(session, token=new_invite_link_token()) is False

    @pytest.mark.asyncio
    async def test_a_deleted_owners_link_resolves_to_nothing(self, db: Session, sessions: Any) -> None:
        owner = create_user(db)
        token = _link(db, owner)
        owner.is_deleted = True
        owner.deleted_at = datetime.now(UTC)
        db.commit()

        async with sessions() as session:
            with _mode(RegistrationMode.INVITE):
                answer = await resolve_invite_link(token, Response(), session)

        assert isinstance(answer, JSONResponse)
        assert answer.status_code == 404


@needs_a_database
class TestTheAuthoritativeGate:
    """`admit_or_refuse`'s link branches against real rows. Each call is rolled back."""

    @staticmethod
    async def _admit(
        sessions: Any, *, email: str, via: str | None, mode: RegistrationMode = RegistrationMode.INVITE
    ) -> Any:
        async with sessions() as session:
            try:
                with _channels(None), _mode(mode):
                    return await admit_or_refuse(session, email=email, via=via)
            finally:
                await session.rollback()

    @pytest.mark.asyncio
    async def test_a_live_link_admits_an_uninvited_address_as_its_owners_invitation(
        self, db: Session, sessions: Any
    ) -> None:
        owner = create_user(db)
        admission = await self._admit(sessions, email=unique_email(), via=_link(db, owner))

        assert admission.source == AccountSource.INVITATION
        assert admission.invite_link_owner_id == owner.id

    @pytest.mark.asyncio
    async def test_the_link_wins_over_an_invitation_from_the_queue(self, db: Session, sessions: Any) -> None:
        owner = create_user(db)
        email = unique_email()
        _invite(db, email=email, inviter=create_user(db, is_super_user=True), from_queue=True)

        admission = await self._admit(sessions, email=email, via=_link(db, owner))

        assert admission.source == AccountSource.INVITATION
        assert admission.invite_link_owner_id == owner.id

    @pytest.mark.asyncio
    async def test_an_owner_at_quota_refuses_an_uninvited_address(self, db: Session, sessions: Any) -> None:
        owner = create_user(db)
        _invite(db, email=unique_email(), inviter=owner)
        token = _link(db, owner)

        with patch.object(settings, "INVITATIONS_PER_USER", 1), pytest.raises(ForbiddenException) as refusal:
            await self._admit(sessions, email=unique_email(), via=token)

        assert refusal.value.detail == SPENT_INVITE_LINK

    @pytest.mark.asyncio
    async def test_an_owner_at_quota_still_lets_in_an_address_somebody_invited(
        self, db: Session, sessions: Any
    ) -> None:
        """Under that invitation's door, and naming no owner, so no link row is written."""
        owner = create_user(db)
        _invite(db, email=unique_email(), inviter=owner)
        token = _link(db, owner)
        email = unique_email()
        _invite(db, email=email, inviter=create_user(db, is_super_user=True), from_queue=True)

        with patch.object(settings, "INVITATIONS_PER_USER", 1):
            admission = await self._admit(sessions, email=email, via=token)

        assert admission.source == AccountSource.WAITLIST
        assert admission.invite_link_owner_id is None

    @pytest.mark.asyncio
    async def test_a_superuser_owner_is_never_at_quota(self, db: Session, sessions: Any) -> None:
        owner = create_user(db, is_super_user=True)
        _invite(db, email=unique_email(), inviter=owner)
        token = _link(db, owner)

        with patch.object(settings, "INVITATIONS_PER_USER", 1):
            admission = await self._admit(sessions, email=unique_email(), via=token)

        assert admission.invite_link_owner_id == owner.id

    @pytest.mark.asyncio
    async def test_a_deleted_owners_link_is_stale(self, db: Session, sessions: Any) -> None:
        owner = create_user(db)
        token = _link(db, owner)
        owner.is_deleted = True
        owner.deleted_at = datetime.now(UTC)
        db.commit()

        with pytest.raises(ForbiddenException) as refusal:
            await self._admit(sessions, email=unique_email(), via=token)

        assert refusal.value.detail == STALE_INVITE_LINK

    @pytest.mark.asyncio
    async def test_a_token_attributes_nothing_on_an_open_instance(self, db: Session, sessions: Any) -> None:
        token = _link(db, create_user(db))

        admission = await self._admit(sessions, email=unique_email(), via=token, mode=RegistrationMode.OPEN)

        assert admission.source == AccountSource.OPEN
        assert admission.invite_link_owner_id is None

    @pytest.mark.asyncio
    async def test_no_via_is_still_not_invited(self, db: Session, sessions: Any) -> None:
        create_user(db)
        with pytest.raises(ForbiddenException) as refusal:
            await self._admit(sessions, email=unique_email(), via=None)

        assert refusal.value.detail == NOT_INVITED


def _completion(email: str, *, via: str | None) -> OnboardingTokenData:
    return OnboardingTokenData(email=email, provider="email", provider_user_id=None, name=None, avatar=None, via=via)


async def _complete(session: AsyncSession, token_data: OnboardingTokenData, *, during_avatar: Any = None) -> None:
    """`POST /auth/complete` for real against `session`, with only what happens after the
    commit stubbed out - `test_join_links.py`'s helper."""
    avatar = {"side_effect": during_avatar} if during_avatar is not None else {"return_value": None}
    with (
        patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
        patch("src.app.api.v1.auth.verify_onboarding_token", new_callable=AsyncMock, return_value=token_data),
        patch("src.app.api.v1.auth.import_google_avatar", new_callable=AsyncMock, **avatar),
        patch("src.app.api.v1.auth.blacklist_token", new_callable=AsyncMock),
        patch(
            "src.app.api.v1.auth.issue_tokens",
            new_callable=AsyncMock,
            return_value={"access_token": "a", "token_type": "bearer"},
        ),
    ):
        await complete_profile(
            fake_request(),
            ProfileCompletionRequest(onboarding_token="irrelevant", name="Grace Reef", username=unique_username()),
            Mock(spec=Response),
            session,
        )


def _invitation_count(db: Session) -> int:
    db.expire_all()
    row = db.get(DailyTotal, (datetime.now(UTC).date(), DailyMetric.ACCOUNTS_CREATED.value, AccountSource.INVITATION))
    return row.count if row is not None else 0


def _rows_for(db: Session, *, inviter: User, email: str) -> list[Invitation]:
    db.expire_all()
    return list(db.scalars(select(Invitation).where(Invitation.user_id == inviter.id, Invitation.email == email)))


@needs_a_database
class TestTheCreatingTransaction:
    @pytest.mark.asyncio
    async def test_a_link_sign_up_gives_the_owner_one_accepted_link_row_in_the_same_commit(
        self, db: Session, sessions: Any
    ) -> None:
        owner, other = create_user(db), create_user(db)
        token = _link(db, owner)
        email = unique_email()
        typed_by_other = _invite(db, email=email, inviter=other)
        db.add(InviteRequest(email=email))
        db.commit()
        before = _invitation_count(db)

        async with sessions() as session:
            with _channels(None), _mode(RegistrationMode.INVITE):
                await _complete(session, _completion(email, via=token))

        created = db.scalars(select(User).where(func.lower(User.email) == email)).one()
        [row] = _rows_for(db, inviter=owner, email=email)
        assert row.from_invite_link is True
        assert row.accepted_at is not None
        assert row.accepted_at >= created.created_at
        assert row.from_invite_request is False
        assert _invitation_count(db) == before + 1
        # Everything else the creating transaction does, as for any other door.
        typed = db.get(Invitation, typed_by_other)
        assert typed is not None
        assert typed.accepted_at is not None
        assert db.scalar(select(func.count()).select_from(InviteRequest).where(InviteRequest.email == email)) == 0
        # The row is charged to the owner's quota.
        async with sessions() as session:
            assert await invitations_created_since(session, user_id=owner.id, window_days=1) == 1

    @pytest.mark.asyncio
    async def test_an_address_the_owner_already_typed_keeps_that_row_and_gains_no_second(
        self, db: Session, sessions: Any
    ) -> None:
        owner = create_user(db)
        token = _link(db, owner)
        email = unique_email()
        typed = _invite(db, email=email, inviter=owner)

        async with sessions() as session:
            with _channels(None), _mode(RegistrationMode.INVITE):
                await _complete(session, _completion(email, via=token))

        [row] = _rows_for(db, inviter=owner, email=email)
        assert row.id == typed
        assert row.from_invite_link is False
        assert row.accepted_at is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("channels", "sentence"), [(None, STALE_INVITE_LINK), ("reddit=Reddit", STALE_JOIN_LINK)])
    async def test_a_link_reset_before_completion_refuses_it_and_creates_nothing(
        self, db: Session, sessions: Any, channels: str | None, sentence: str
    ) -> None:
        """`test_a_revocation_committed_mid_handler_is_honoured`'s shape: the owner resets the
        link from another session while the handler is between its duplicate checks and its
        insert, and the gate below the rollback reads the row as it is by then."""
        owner = create_user(db)
        token = _link(db, owner)
        email = unique_email()

        async def reset_from_another_session(_: Any) -> None:
            async with sessions() as other:
                await other.execute(
                    update(InviteLink).where(InviteLink.user_id == owner.id).values(token=new_invite_link_token())
                )
                await other.commit()

        async with sessions() as session:
            with (
                _channels(channels),
                _mode(RegistrationMode.INVITE),
                pytest.raises(ForbiddenException) as refusal,
            ):
                await _complete(session, _completion(email, via=token), during_avatar=reset_from_another_session)

        assert refusal.value.detail == sentence
        assert db.scalar(select(func.count()).select_from(User).where(func.lower(User.email) == email)) == 0
        assert _rows_for(db, inviter=owner, email=email) == []

    @pytest.mark.asyncio
    async def test_an_owner_at_quota_refuses_and_creates_nothing(self, db: Session, sessions: Any) -> None:
        owner = create_user(db)
        _invite(db, email=unique_email(), inviter=owner)
        token = _link(db, owner)
        email = unique_email()

        async with sessions() as session:
            with (
                _channels(None),
                _mode(RegistrationMode.INVITE),
                patch.object(settings, "INVITATIONS_PER_USER", 1),
                pytest.raises(ForbiddenException) as refusal,
            ):
                await _complete(session, _completion(email, via=token))

        assert refusal.value.detail == SPENT_INVITE_LINK
        assert db.scalar(select(func.count()).select_from(User).where(func.lower(User.email) == email)) == 0


@needs_a_database
class TestWhatTheInviterSees:
    @staticmethod
    async def _listed(sessions: Any, inviter: User) -> list[dict[str, Any]]:
        async with sessions() as session:
            with _mode(RegistrationMode.INVITE):
                page = await read_invitations(_caller(inviter), session)
        return list(page["data"])

    @pytest.mark.asyncio
    async def test_a_link_row_names_the_member_and_a_typed_row_is_unchanged(self, db: Session, sessions: Any) -> None:
        owner = create_user(db)
        token = _link(db, owner)
        typed_email = unique_email()
        _invite(db, email=typed_email, inviter=owner)
        joined_email = unique_email()

        async with sessions() as session:
            with _channels(None), _mode(RegistrationMode.INVITE):
                await _complete(session, _completion(joined_email, via=token))

        joined, typed = await self._listed(sessions, owner)
        assert joined["email"] is None
        assert joined["from_invite_link"] is True
        assert joined["member_name"] == "Grace Reef"
        assert typed["email"] == typed_email
        assert typed["from_invite_link"] is False
        assert typed["member_name"] is None

        # The current name, as the member changes it.
        db.execute(update(User).where(func.lower(User.email) == joined_email).values(name="Grace Hopper"))
        db.commit()
        assert (await self._listed(sessions, owner))[0]["member_name"] == "Grace Hopper"

    @pytest.mark.asyncio
    async def test_a_later_account_on_the_old_address_is_never_named(self, db: Session, sessions: Any) -> None:
        """The joiner moves to another address, and somebody who never opened the link signs
        up with the old one: the row names nobody rather than them."""
        owner = create_user(db)
        token = _link(db, owner)
        email = unique_email()

        async with sessions() as session:
            with _channels(None), _mode(RegistrationMode.INVITE):
                await _complete(session, _completion(email, via=token))

        db.execute(update(User).where(func.lower(User.email) == email).values(email=unique_email()))
        db.commit()
        stranger = create_user(db)
        stranger.email = email
        stranger.created_at = datetime.now(UTC) + timedelta(seconds=1)
        db.commit()

        [row] = await self._listed(sessions, owner)
        assert row["member_name"] is None
        assert row["email"] is None
