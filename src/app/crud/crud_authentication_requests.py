from datetime import UTC, datetime
from typing import cast

from fastcrud import FastCRUD
from sqlalchemy import CursorResult, case, null, update
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


async def register_failed_code_attempt(db: AsyncSession, *, request_id: int, max_attempts: int) -> None:
    """Charge one wrong guess against a row's sign-in code, and spend the code outright
    once `max_attempts` of them have been charged.

    The attempt cap is the *only* thing standing between a six-digit code and whoever
    holds the `request_id` it belongs to, so it has to hold under concurrency. Read the
    counter, decide, then write it back and two simultaneous guesses both read the same
    number and both store `n + 1` - an attacker firing guesses in parallel is charged for
    a fraction of them, which is precisely the shape that turns a 5-in-a-million bound
    into no bound at all. One `UPDATE` that increments and nulls in the same expression
    has no window to lose: Postgres evaluates `code_attempts + 1` against the row version
    it holds the lock on, so concurrent statements queue and each is counted.

    Nulling `code_hash` kills **the code and nothing else**. The link in the same email is
    untouched, on purpose: it carries 256 bits delivered to a mailbox and needs no
    protection from code guessing, whereas voiding it here would hand anyone who can
    reach this endpoint a way to cancel a sign-in they cannot complete - aimed squarely at
    the accounts whose only method is email.

    No `WHERE code_hash IS NOT NULL`: the caller has already established there is a live
    code, and a row whose code is spent is one the caller rejects before reaching here.
    """
    await db.execute(
        update(AuthenticationRequest)
        .where(AuthenticationRequest.id == request_id)
        .values(
            code_attempts=AuthenticationRequest.code_attempts + 1,
            code_hash=case(
                (AuthenticationRequest.code_attempts + 1 >= max_attempts, null()),
                else_=AuthenticationRequest.code_hash,
            ),
        )
    )
    await db.commit()
