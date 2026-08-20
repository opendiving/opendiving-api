from datetime import UTC, datetime
from typing import cast

from fastcrud import FastCRUD
from sqlalchemy import CursorResult, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.authentication_request import AuthenticationRequest
from ..schemas.authentication_request import (
    AuthenticationRequestCreate,
    AuthenticationRequestRead,
    AuthenticationRequestUpdate,
)

CRUDAuthenticationRequest = FastCRUD[
    AuthenticationRequest,
    AuthenticationRequestCreate,
    AuthenticationRequestUpdate,
    AuthenticationRequestUpdate,
    AuthenticationRequestUpdate,
    AuthenticationRequestRead,
]
crud_authentication_requests = CRUDAuthenticationRequest(AuthenticationRequest)


async def claim_authentication_request(db: AsyncSession, *, request_id: int, commit: bool = True) -> bool:
    """Stamp `used_at` on a request, returning whether *this* caller is the one that did
    it. `False` means someone else got there first, and the caller must not proceed.

    This is what actually makes a token single-use. Both verify endpoints read the row
    first and reject an already-`used_at` one, but a read and a later write are two
    statements: two concurrent submissions of the same link both see `used_at IS NULL`,
    both pass that gate, and both go on to do whatever verifying the link does. The
    read-based check stays as a fast path - it gives the caller the right reason without a
    pointless write - but this is the authoritative gate, so call it immediately before
    the thing it authorizes.

    Hand-written Core rather than `crud_authentication_requests.update(..., used_at=None)`
    with `NoResultFound` caught, which looks equivalent and is not: FastCRUD implements
    that zero-match check as a `count()` issued *before* the UPDATE
    (`fastcrud.crud.validation.validate_update_delete_operation`) and then discards the
    statement's own `rowcount`, so it is the same check-then-act in a narrower window. See
    *"A filter on a FastCRUD `update` is a `count()`, not an atomic condition"* in
    `DECISIONS.md`.

    `rowcount == 0` is a correct and sufficient race-lost signal under Postgres's default
    READ COMMITTED, which is not obvious: the loser blocks on the winner's row lock, and
    once the winner commits it re-evaluates the `WHERE` predicate against the committed
    new row version, matches nothing, and reports zero. No `SELECT ... FOR UPDATE` and no
    isolation-level change is needed.
    """
    result = cast(
        CursorResult,
        await db.execute(
            update(AuthenticationRequest)
            .where(AuthenticationRequest.id == request_id, AuthenticationRequest.used_at.is_(None))
            .values(used_at=datetime.now(UTC))
        ),
    )
    # Read before committing: the count belongs to the statement, not to the transaction.
    claimed = result.rowcount > 0
    if commit:
        await db.commit()
    return claimed
