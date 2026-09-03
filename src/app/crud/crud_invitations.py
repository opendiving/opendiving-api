from datetime import UTC, datetime, timedelta
from typing import cast

from fastcrud import FastCRUD
from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

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


async def invitations_created_since(db: AsyncSession, *, user_id: int, window_days: int) -> int:
    """How many invitations this account has caused to be sent in the trailing window.

    **Revoked rows are counted**, which is the point of counting rows rather than live
    invitations: the quota bounds emails sent, and revoking one after the send does not
    unsend it. Accepted ones count too, for the same reason.

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
    """Stamp `accepted_at` on every live, unaccepted invitation for `email`.

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
            .values(accepted_at=datetime.now(UTC))
        ),
    )
    # Read before any commit: the count belongs to the statement, not to the transaction.
    accepted = result.rowcount
    if commit:
        await db.commit()
    return accepted


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
