from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastcrud import FastCRUD
from sqlalchemy import CursorResult, case, func, select, update
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


async def list_invitations_from(db: AsyncSession, *, user_id: int, offset: int, limit: int) -> dict[str, Any]:
    """One page of an inviter's rows, newest first, in `get_multi`'s `{data, total_count}`
    shape - each row carrying, on a link row only, the name of the account it admitted.

    That account is the one whose `lower(email)` is the row's address **and whose
    `created_at` is no later than the row's `accepted_at`**: the row is written in the
    transaction that creates the account, after its insert. The accepted row keeps the
    address admissible, so once the joiner moves to another address somebody else may sign
    up with this one - and that person never opened the link, so the time bound keeps them
    off the inviter's card. A correlated subquery taking the newest such account rather than
    a join, because `lower(User.email)` is not unique and a join could repeat a row.
    """
    member_name = (
        select(User.name)
        .where(func.lower(User.email) == Invitation.email, User.created_at <= Invitation.accepted_at)
        .order_by(User.created_at.desc())
        .limit(1)
        .correlate(Invitation)
        .scalar_subquery()
    )
    rows = (
        await db.execute(
            select(
                Invitation.uuid,
                Invitation.email,
                Invitation.created_at,
                Invitation.accepted_at,
                Invitation.revoked_at,
                Invitation.from_invite_link,
                case((Invitation.from_invite_link, member_name), else_=None).label("member_name"),
            )
            .where(Invitation.user_id == user_id)
            .order_by(Invitation.created_at.desc(), Invitation.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).mappings()
    total = await db.scalar(select(func.count()).select_from(Invitation).where(Invitation.user_id == user_id))
    return {"data": [dict(row) for row in rows], "total_count": int(total or 0)}


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

    The 409 on `POST /user/invitations`, and the batch route's per-inviter skip. Scoped to
    one inviter because the table deliberately is not unique on the address: two members
    may each invite the same friend, and each sees their own row.
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
    invitations made at once - two typed, or one typed and a sign-up through the member's
    link - would both count the same number and both be let through past the quota. Taken
    before the count, the second waits for the first's transaction to end and counts its
    row. Released by the transaction ending, whichever way it ends.
    """
    await db.execute(select(func.pg_advisory_xact_lock(_INVITATION_QUOTA_LOCK, user_id)))


async def invitations_created_since(db: AsyncSession, *, user_id: int, window_days: int) -> int:
    """How many invitations this account has made in the trailing window, by address or
    through its invite link.

    **Revoked rows are counted**, which is the point of counting rows rather than live
    invitations: the quota bounds invitations made, and revoking one after the send does not
    unsend it. Accepted ones count too, for the same reason - which is how a sign-up through
    the member's link, recorded as an accepted row, is charged to the same allowance.

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

    Every live row rather than one: two members may have invited the same friend, and both
    of them should see that the friend arrived. Revoked rows are left alone - whoever
    revoked did not invite this account in.

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


async def record_invite_link_sign_up(db: AsyncSession, *, email: str, inviter_id: int) -> None:
    """Give the member whose invite link admitted `email` the accepted row that records it.

    Called inside `POST /auth/complete`'s creating transaction, after the `User` row is built
    and before `accept_invitations`, and commits nothing. Where the member had already typed
    a live invitation to this address, nothing is written: that row is stamped accepted by
    `accept_invitations` and stays a typed row showing the address they typed, so the member
    ends with exactly one accepted row for it either way.

    `accepted_at` is taken here rather than passed in, so it is never earlier than the new
    account's `created_at` - the bound `GET /user/invitations` uses to name the account a link
    row admitted.
    """
    if await live_invitation_from(db, email=email, user_id=inviter_id):
        return
    db.add(
        Invitation(
            email=email,
            user_id=inviter_id,
            accepted_at=datetime.now(UTC),
            from_invite_link=True,
        )
    )
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
