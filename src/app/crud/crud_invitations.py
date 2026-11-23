from datetime import UTC, datetime, timedelta
from typing import cast

from fastcrud import FastCRUD
from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import AccountSource
from ..models.invitation import Invitation
from ..models.user import User
from ..schemas.invitation import InvitationCreateInternal, InvitationReadInternal, InvitationUpdate

CRUDInvitation = FastCRUD[
    Invitation,
    InvitationCreateInternal,
    InvitationUpdate,
    InvitationUpdate,
    InvitationUpdate,
    InvitationReadInternal,
]
crud_invitations = CRUDInvitation(Invitation)


async def live_invitation_exists(db: AsyncSession, *, email: str) -> bool:
    """Whether `email` has an invitation nobody has revoked - the gate's whole predicate.

    `email` must already be lowercased: the column is written lowercase everywhere and this
    is an equality, not an `ILIKE`. `services.registration_gate` is the only caller that
    matters and it normalises first, which is what keeps a Google claim carrying capitals
    from missing its own invitation.

    An *accepted* invitation still counts as live here, and that is deliberate rather than
    an oversight. The only caller runs immediately before creating an account, and by then
    the duplicate-email check has already refused an address that has one - so the accepted
    row can only belong to a purged account whose invitation outlived it, which is exactly
    the case where re-admitting the address is right.
    """
    exists = await db.scalar(
        select(func.count()).select_from(Invitation).where(Invitation.email == email, Invitation.revoked_at.is_(None))
    )
    return bool(exists)


async def invitation_source(db: AsyncSession, *, email: str) -> AccountSource | None:
    """The door an invited address is counted under, or `None` when nothing invites it.

    `live_invitation_exists`'s predicate, read once more for the flag beside it: `waitlist`
    when the operator sent any of the address's invitations from the queue, `invitation`
    otherwise. `bool_or` over no rows is `NULL`, which is the "nothing invites it" answer.
    `email` must already be lowercased, for the reason given there.
    """
    from_queue = await db.scalar(
        select(func.bool_or(Invitation.from_invite_request)).where(
            Invitation.email == email, Invitation.revoked_at.is_(None)
        )
    )
    if from_queue is None:
        return None
    return AccountSource.WAITLIST if from_queue else AccountSource.INVITATION


async def live_invitation_from(db: AsyncSession, *, email: str, user_id: int) -> bool:
    """Whether this inviter already has a live, unaccepted invitation out to `email`.

    The batch route's per-inviter skip, and what keeps a connect-link sign-up from writing a
    second row beside one the member typed. Scoped to one inviter because the table
    deliberately is not unique on the address: two inviters may each have invited the same
    friend.
    """
    exists = await db.scalar(
        select(func.count())
        .select_from(Invitation)
        .where(
            Invitation.email == email,
            Invitation.user_id == user_id,
            Invitation.revoked_at.is_(None),
            Invitation.accepted_at.is_(None),
        )
    )
    return bool(exists)


# The namespace of the per-inviter lock `hold_invitation_quota` takes, keyed `(this, user_id)`.
# The two-integer form of `pg_advisory_xact_lock` occupies a keyspace of its own - `pg_locks`
# tells the two apart by `objsubid` - so it cannot collide with the single-`bigint` keys
# `core.setup` and `services.registration_gate` take, whatever their values.
_INVITATION_QUOTA_LOCK = 0x0D1E_0002


async def hold_invitation_quota(db: AsyncSession, *, user_id: int) -> None:
    """Take the transaction-scoped lock one account's invitation quota is counted under.

    `invitations_created_since` then the insert is a count and a later write, and two
    sign-ups through the member's connect link at once would both count the same number and
    both be let through past the quota. Taken
    before the count, the second waits for the first's transaction to end and counts its
    row. Released by the transaction ending, whichever way it ends.
    """
    await db.execute(select(func.pg_advisory_xact_lock(_INVITATION_QUOTA_LOCK, user_id)))


async def invitations_created_since(db: AsyncSession, *, user_id: int, window_days: int) -> int:
    """How many invitation rows this account holds from the trailing window: each sign-up
    through its connect link writes an accepted one, and any other row it holds there is
    counted whatever its stamps, since the quota bounds invitations made rather than live ones.

    Counted from the table rather than from the Redis rate limiter, because that limiter
    fails open on a Redis outage by design (`DECISIONS.md` §"Rate limiting fails open on a
    Redis *outage*") and its counters are flushed locally. A limit that decides who gets
    into a closed instance should not inherit either property.
    """
    since = datetime.now(UTC) - timedelta(days=window_days)
    count = await db.scalar(
        select(func.count())
        .select_from(Invitation)
        .where(Invitation.user_id == user_id, Invitation.created_at >= since)
    )
    return int(count or 0)


async def accept_invitations(db: AsyncSession, *, email: str, commit: bool = False) -> int:
    """Stamp `accepted_at` on every live, unaccepted invitation for `email`, clearing
    `from_invite_request` in the same statement.

    The flag has been read by then - the gate, earlier in the same transaction - and an
    accepted row is never swept, so keeping it would make "this address asked to be let
    in" a fact about the account for as long as the account lasts.

    Called with `commit=False` from inside `POST /auth/complete`'s creating transaction, so
    that "account created" and "invitation accepted" land or roll back together - the
    invariant the gate rests on, since a committed account beside an unaccepted invitation
    would leave the address admissible a second time.

    Every live row rather than one: two inviters may have invited the same friend, and each
    row then records the account it let in. A revoked row is left alone - it admits nobody.

    Hand-written Core rather than `crud_invitations.update(..., allow_multiple=True)`, for
    the reason `claim_authentication_request` records: FastCRUD implements its zero-match
    check as a `count()` issued before the UPDATE and raises `NoResultFound` when nothing
    matches, which here is the ordinary case (an account created through the bootstrap
    exemption has no invitation at all).
    """
    result = cast(
        CursorResult,
        await db.execute(
            update(Invitation)
            .where(Invitation.email == email, Invitation.revoked_at.is_(None), Invitation.accepted_at.is_(None))
            .values(accepted_at=datetime.now(UTC), from_invite_request=False)
        ),
    )
    # Read before any commit: the count belongs to the statement, not to the transaction.
    accepted = result.rowcount
    if commit:
        await db.commit()
    return accepted


async def record_connect_link_sign_up(db: AsyncSession, *, email: str, inviter_id: int) -> None:
    """Give the member whose connect link admitted `email` the accepted row that records it.

    Called inside `POST /auth/complete`'s creating transaction, after the `User` row is built
    and before `accept_invitations`, and commits nothing. Where the member had already typed
    a live invitation to this address, nothing is written: `accept_invitations` stamps that
    row accepted, so the member ends with exactly one accepted row for it either way, and the
    quota counts one invitation.
    """
    if await live_invitation_from(db, email=email, user_id=inviter_id):
        return
    db.add(Invitation(email=email, user_id=inviter_id, accepted_at=datetime.now(UTC)))
    await db.flush()


async def account_exists_for(db: AsyncSession, *, email: str) -> bool:
    """Whether a live account holds `email`, compared **case-insensitively**.

    `lower(User.email)` rather than a plain equality, and that is the whole reason this
    helper exists. `POST /auth/complete` inserts the onboarding token's address verbatim,
    and the Google path never lowercased its claim, so a stored `User.email` may carry
    capitals - while both invitation tables store lowercase. Comparing a lowercased
    invitation address against the raw column would miss exactly those accounts, which is
    how an inviter would be told an address is free when it is not.

    Deleted accounts count as existing: the row is still there, its address is still taken
    by the unique index, and it can be restored inside its grace period.
    """
    exists = await db.scalar(select(func.count()).select_from(User).where(func.lower(User.email) == email))
    return bool(exists)
