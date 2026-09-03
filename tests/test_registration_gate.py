"""The registration gate: may this verified address create an account right now?

Two halves, and the split is the point rather than an artefact of what was easy to write.

The **predicate** is unit-tested against a mocked session: mode, invitation, emptiness, and
the case-folding of a Google claim are all decisions in Python and a database would only
slow them down.

The **transactional** claims are not testable that way at all, and they are the two this
node actually rests on: that the gate's answer is still true at the moment the row is
inserted, and that two first sign-ups against an empty table cannot both be admitted as the
operator. A mocked session evaluates no `WHERE`, holds no lock and has no second connection
to race, so those run against a real Postgres with two sessions - the shape
`test_authentication_request_claim.py` established for `claim_authentication_request`. A
structural assertion that "the lock is taken" would pass on a build that took it above
`release_read_transaction`, where the rollback discards it, which is precisely the bug.

Automatically skipped when no database is reachable - note that a run on the host needs
`POSTGRES_SERVER=localhost` before these execute at all (see `CONTRIBUTING.md`).
"""

import asyncio
import uuid as uuid_pkg
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1.auth import complete_profile
from src.app.core.config import RegistrationMode, settings
from src.app.core.exceptions.http_exceptions import ForbiddenException
from src.app.core.schemas import OnboardingTokenData
from src.app.models.invitation import Invitation
from src.app.models.user import User
from src.app.schemas.auth import ProfileCompletionRequest
from src.app.services.registration_gate import NOT_INVITED, admit_or_refuse, refuse_uninvited
from tests.conftest import db_available, unique_email, unique_username
from tests.helpers.generators import create_user
from tests.helpers.mocks import awaited_kwargs

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")


def _mode(mode: RegistrationMode) -> Any:
    return patch.object(settings, "REGISTRATION_MODE", mode)


def _request() -> Mock:
    request = Mock()
    request.client = Mock(host="203.0.113.7")
    request.headers = {}
    return request


def _invitation(db: Session, *, email: str, inviter: User, revoked: bool = False) -> Invitation:
    row = Invitation(
        email=email,
        user_id=inviter.id,
        uuid=uuid7(),
        revoked_at=datetime.now(UTC) if revoked else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class TestThePredicate:
    """`refuse_uninvited`, the advisory check at the onboarding branch. Mocked session: what
    is being asserted is which questions get asked and what is done with the answers."""

    @pytest.mark.asyncio
    async def test_open_mode_asks_nothing_at_all(self, mock_db) -> None:
        """Not merely "admits": the point is that an open instance runs no invitation query
        and no emptiness count, so the feature is absent rather than idle."""
        with (
            _mode(RegistrationMode.OPEN),
            patch("src.app.services.registration_gate.live_invitation_exists", new_callable=AsyncMock) as invited,
        ):
            await refuse_uninvited(mock_db, email="stranger@example.com")

        invited.assert_not_awaited()
        mock_db.scalar.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_invited_address_passes(self, mock_db) -> None:
        with (
            _mode(RegistrationMode.INVITE),
            patch(
                "src.app.services.registration_gate.live_invitation_exists", new_callable=AsyncMock, return_value=True
            ),
        ):
            await refuse_uninvited(mock_db, email="invited@example.com")

    @pytest.mark.asyncio
    async def test_an_uninvited_address_is_refused_with_the_shared_sentence(self, mock_db) -> None:
        """One sentence at both gate sites, so the person who followed their link and the
        person who submitted the profile form read the same thing."""
        mock_db.scalar = AsyncMock(return_value=4)

        with (
            _mode(RegistrationMode.INVITE),
            patch(
                "src.app.services.registration_gate.live_invitation_exists", new_callable=AsyncMock, return_value=False
            ),
            pytest.raises(ForbiddenException) as refusal,
        ):
            await refuse_uninvited(mock_db, email="stranger@example.com")

        assert refusal.value.detail == NOT_INVITED

    @pytest.mark.asyncio
    async def test_an_empty_instance_admits_anyone(self, mock_db) -> None:
        """The bootstrap has to be able to reach onboarding like anybody else, or a fresh
        invite-mode instance is a deadlock: no invitations, and no superuser to make one."""
        mock_db.scalar = AsyncMock(return_value=0)

        with (
            _mode(RegistrationMode.INVITE),
            patch(
                "src.app.services.registration_gate.live_invitation_exists", new_callable=AsyncMock, return_value=False
            ),
        ):
            await refuse_uninvited(mock_db, email="operator@example.com")

    @pytest.mark.asyncio
    async def test_the_address_is_lowercased_before_it_is_looked_up(self, mock_db) -> None:
        """Google hands `resolve_identity` its `email` claim as it came, un-lowercased, and
        both invitation tables store lowercase. Comparing the claim raw is how
        `Diver@Example.com` misses its own invitation."""
        with (
            _mode(RegistrationMode.INVITE),
            patch(
                "src.app.services.registration_gate.live_invitation_exists", new_callable=AsyncMock, return_value=True
            ) as invited,
        ):
            await refuse_uninvited(mock_db, email="Diver@Example.COM")

        assert awaited_kwargs(invited)["email"] == "diver@example.com"


class TestTheGateIsInsideTheCreatingTransaction:
    """Invariant 2's transactional half, which is the one a mock cannot reach.

    `complete_profile` calls `release_read_transaction` - a **rollback** - between its
    duplicate checks and its insert. A gate query taken above that line is discarded before
    the row is written, so what has to be true is that a revocation another session commits
    *after* the handler's duplicate checks is still honoured. Staged for real, on two
    connections.
    """

    @pytest_asyncio.fixture(autouse=True)
    async def _clean_slate(self, db: Session) -> AsyncGenerator[None]:
        yield
        db.rollback()

    @pytest.mark.asyncio
    async def test_a_revocation_committed_mid_handler_is_honoured(self, db: Session, diver: User) -> None:
        """The race the placement exists for, and the one an assertion about lock ordering
        cannot substitute for.

        The revoke is committed by a *second* session while the handler is between its
        duplicate checks and its insert - which is exactly the window
        `release_read_transaction` opens, since the handler goes out to fetch a Google
        avatar there. Under READ COMMITTED the gate's own `SELECT`, running below the
        rollback, sees the committed new row version and refuses.
        """
        email = unique_email()
        invitation = _invitation(db, email=email, inviter=diver)
        invitation_id = invitation.id
        db.expunge(invitation)

        engine = create_async_engine(settings.POSTGRES_ASYNC_PREFIX + settings.POSTGRES_URI)
        sessions = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

        async def revoke_from_another_session(_: Any) -> None:
            """Stands in for the inviter clicking Revoke in another tab. Hooked onto the
            avatar import because that is what the handler is doing while the transaction
            is released - the whole reason the window exists."""
            async with sessions() as other:
                await other.execute(
                    update(Invitation).where(Invitation.id == invitation_id).values(revoked_at=datetime.now(UTC))
                )
                await other.commit()

        try:
            async with sessions() as handler_db:
                with (
                    _mode(RegistrationMode.INVITE),
                    patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
                    patch(
                        "src.app.api.v1.auth.verify_onboarding_token",
                        new_callable=AsyncMock,
                        return_value=OnboardingTokenData(
                            email=email, provider="email", provider_user_id=None, name=None, avatar=None
                        ),
                    ),
                    patch("src.app.api.v1.auth.import_google_avatar", side_effect=revoke_from_another_session),
                    pytest.raises(ForbiddenException) as refusal,
                ):
                    await complete_profile(
                        _request(),
                        ProfileCompletionRequest(
                            onboarding_token="irrelevant", name="Ada Reef", username=unique_username()
                        ),
                        Mock(),
                        handler_db,
                    )

            assert refusal.value.detail == NOT_INVITED
        finally:
            async with sessions() as cleanup:
                await cleanup.execute(delete(Invitation).where(Invitation.id == invitation_id))
                await cleanup.commit()
            await engine.dispose()

        assert db.query(User).filter(func.lower(User.email) == email).count() == 0

    @pytest.mark.asyncio
    async def test_the_refusal_releases_the_advisory_lock(self, db: Session, diver: User) -> None:
        """The reason the handler has an `except ForbiddenException` that rolls back.

        `pg_advisory_xact_lock` lives until the transaction ends and `async_get_db` does not
        end one on unwind, so a refusal that left it held would block every other account
        creation for as long as the connection sat in the pool. Asserted by taking the same
        lock from a second session immediately afterwards, with `try` semantics so a still
        held lock is a `False` rather than a hang.
        """
        engine = create_async_engine(settings.POSTGRES_ASYNC_PREFIX + settings.POSTGRES_URI)
        sessions = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

        try:
            async with sessions() as handler_db:
                with (
                    _mode(RegistrationMode.INVITE),
                    patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
                    patch(
                        "src.app.api.v1.auth.verify_onboarding_token",
                        new_callable=AsyncMock,
                        return_value=OnboardingTokenData(
                            email=unique_email(), provider="email", provider_user_id=None, name=None, avatar=None
                        ),
                    ),
                    patch("src.app.api.v1.auth.import_google_avatar", new_callable=AsyncMock, return_value=None),
                    pytest.raises(ForbiddenException),
                ):
                    await complete_profile(
                        _request(),
                        ProfileCompletionRequest(
                            onboarding_token="irrelevant", name="Ada Reef", username=unique_username()
                        ),
                        Mock(),
                        handler_db,
                    )

                async with sessions() as observer:
                    from src.app.services.registration_gate import _REGISTRATION_LOCK_KEY

                    taken = await observer.scalar(select(func.pg_try_advisory_xact_lock(_REGISTRATION_LOCK_KEY)))
                    await observer.rollback()

            assert taken is True
        finally:
            await engine.dispose()


class TestTheBootstrapExemption:
    """Invariant 3, against an empty `user` table - which the suite's own database is not,
    so these run inside a transaction that deletes every account and is rolled back.

    That is the only way to stage it honestly. The exemption's condition is "the table is
    empty", not "a flag is unset", so a fixture that mocked the count would be testing the
    mock. `SERIALIZABLE` is not needed and would obscure the thing under test: what is being
    asserted is that the advisory lock, not the isolation level, is what makes two
    concurrent first completions produce one superuser.
    """

    @pytest_asyncio.fixture
    async def empty_instance(self) -> AsyncGenerator[Any]:
        """A session factory whose transactions see no accounts.

        Every account is deleted inside each session's own transaction and never committed,
        so the developer's own rows are untouched - the delete is rolled back on the way
        out. Two sessions opened this way both see an empty table, which is exactly the
        fresh-instance race.
        """
        engine = create_async_engine(settings.POSTGRES_ASYNC_PREFIX + settings.POSTGRES_URI)
        yield async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        await engine.dispose()

    @staticmethod
    async def _hide_every_account(session: AsyncSession) -> None:
        """Inside this transaction only. Never committed by these tests."""
        await session.execute(delete(User))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", [RegistrationMode.INVITE, RegistrationMode.OPEN])
    async def test_an_empty_table_admits_and_says_so(self, empty_instance: Any, mode: RegistrationMode) -> None:
        """`True` means "this is the first account", which is what `complete_profile` turns
        into `is_superuser`. It holds in both modes: a fresh *open* instance also gets an
        operator without SQL."""
        async with empty_instance() as session:
            await self._hide_every_account(session)

            with _mode(mode):
                assert await admit_or_refuse(session, email=unique_email()) is True

            await session.rollback()

    @pytest.mark.asyncio
    async def test_a_populated_table_refuses_an_uninvited_address(self, empty_instance: Any, diver: User) -> None:
        """The other side of the same call, and what makes the exemption an exemption rather
        than a hole: with one account in the table the gate is back on."""
        async with empty_instance() as session:
            with _mode(RegistrationMode.INVITE), pytest.raises(ForbiddenException) as refusal:
                await admit_or_refuse(session, email=unique_email())

            assert refusal.value.detail == NOT_INVITED
            await session.rollback()

    @pytest.mark.asyncio
    async def test_a_soft_deleted_account_still_counts_as_an_instance(self, db: Session, empty_instance: Any) -> None:
        """An account inside its deletion grace period holds its address and can be
        restored, so an instance holding one is not a fresh instance. The exemption comes
        back only after the purge hard-deletes the row."""
        pending = create_user(db)
        pending.is_deleted = True
        pending.deleted_at = datetime.now(UTC)
        db.commit()

        async with empty_instance() as session:
            await session.execute(delete(User).where(User.id != pending.id))

            with _mode(RegistrationMode.INVITE), pytest.raises(ForbiddenException):
                await admit_or_refuse(session, email=unique_email())

            await session.rollback()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", [RegistrationMode.INVITE, RegistrationMode.OPEN])
    async def test_two_concurrent_first_completions_produce_exactly_one_superuser(
        self, empty_instance: Any, mode: RegistrationMode
    ) -> None:
        """The race the advisory lock exists for, run for real on two connections.

        Both gates are entered before either transaction commits. Without the lock both
        would count zero accounts and both would be admitted as the operator; with it the
        second blocks, and when it proceeds it sees the winner's committed row.

        Committed on purpose here, unlike the tests above - the loser has to observe the
        winner's row, and an uncommitted insert is invisible to another connection. Both
        accounts are removed in the `finally`, and the two addresses are fresh, so nothing
        of the developer's is touched.

        **The mode patch wraps the `gather`, not each racer.** `patch.object` mutates one
        module-level `settings`, so a `with` inside each coroutine has the loser tearing the
        patch down while the winner is still inside it - which silently ran the open-mode
        case under the `invite` default and made it look like the lock had refused somebody
        it should have admitted.
        """
        first, second = unique_email(), unique_email()
        created: list[int] = []

        async def complete(email: str) -> bool | str:
            async with empty_instance() as session:
                await self._hide_every_account(session)
                try:
                    bootstrap = await admit_or_refuse(session, email=email)
                except ForbiddenException:
                    await session.rollback()
                    return "refused"

                row = User(
                    name="Racer",
                    username=unique_username(),
                    email=email,
                    uuid=uuid_pkg.uuid4(),
                    is_superuser=bootstrap,
                )
                session.add(row)
                await session.commit()
                created.append(row.id)
                return bootstrap

        try:
            with _mode(mode):
                outcomes = await asyncio.gather(complete(first), complete(second))

            assert outcomes.count(True) == 1, f"exactly one bootstrap expected, got {outcomes}"
            if mode is RegistrationMode.INVITE:
                # The loser has no invitation and the table is no longer empty.
                assert outcomes.count("refused") == 1
            else:
                # Open mode creates the loser as an ordinary account.
                assert outcomes.count(False) == 1
        finally:
            async with empty_instance() as cleanup:
                if created:
                    await cleanup.execute(delete(User).where(User.id.in_(created)))
                    await cleanup.commit()
