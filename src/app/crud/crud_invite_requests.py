from typing import cast

from fastcrud import FastCRUD
from sqlalchemy import CursorResult, delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.invite_request import InviteRequest
from ..schemas.invite_request import InviteRequestCreateInternal

CRUDInviteRequest = FastCRUD[
    InviteRequest,
    InviteRequestCreateInternal,
    InviteRequestCreateInternal,
    InviteRequestCreateInternal,
    InviteRequestCreateInternal,
    InviteRequestCreateInternal,
]
crud_invite_requests = CRUDInviteRequest(InviteRequest)


async def record_invite_request(db: AsyncSession, *, email: str, commit: bool = True) -> None:
    """Store a pending request for `email`, or do nothing if one is already there.

    `ON CONFLICT DO NOTHING` rather than a `SELECT` followed by an `INSERT`, and the
    difference is the endpoint's guarantee rather than an optimisation. A check-then-insert
    has a window in which two submissions of the same address both find nothing and both
    insert, and the second would raise an `IntegrityError` the handler would then have to
    turn back into the same neutral `202` - a branch that can be got wrong, on the one
    endpoint in this app where being got wrong means telling a stranger whether an address
    is already in the queue. One statement has no window and one answer.

    Postgres-specific `insert`, like nothing else in this app so far, because the generic
    Core `insert()` has no `on_conflict_do_nothing`. That costs no portability the app had:
    `postgresql://` is the only DSN `core.config` builds.
    """
    await db.execute(insert(InviteRequest).values(email=email).on_conflict_do_nothing(index_elements=["email"]))
    if commit:
        await db.commit()


async def delete_invite_requests(db: AsyncSession, *, emails: list[str], commit: bool = True) -> int:
    """Drop the named addresses from the queue, returning how many rows actually went.

    Also the statement that runs inside an invitation's own transaction, with
    `commit=False`, for the single address being invited - which is what makes "an address
    with a live invitation has no request row" true at every instant rather than between
    two commits.

    Core rather than `crud_invite_requests.delete()`, for the reason
    `purge_expired_authentication_requests` gives: FastCRUD raises `NoResultFound` when
    nothing matches, and nothing matching is the ordinary case here (an operator inviting
    an address that never asked).
    """
    if not emails:
        return 0

    result = cast(CursorResult, await db.execute(delete(InviteRequest).where(InviteRequest.email.in_(emails))))
    # Read before any commit: the count belongs to the statement, not to the transaction.
    removed = result.rowcount
    if commit:
        await db.commit()
    return removed
