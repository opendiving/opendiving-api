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


async def charge_code_attempt(db: AsyncSession, *, request_id: int, max_attempts: int) -> str | None:
    """Spend one of a row's sign-in code attempts and return the digest to compare the
    submitted code against - or `None` when there is no attempt left to spend, which the
    caller answers exactly as it answers a wrong code.

    The attempt cap is the *only* thing standing between a six-digit code and whoever
    holds the `request_id` it belongs to, so it has to hold under concurrency, and that is
    why the charge comes **before** the comparison rather than after a wrong one. Compare
    first and charge on failure, and every guess in flight has already read the digest
    before any of their charges lands: an attacker firing a hundred guesses together has a
    hundred codes evaluated, and the one that matches goes on to claim the row whatever the
    counter says by then. Here the only way to see the digest is through this `UPDATE`,
    whose `code_attempts < max_attempts` is evaluated against the row version it holds the
    lock on, so concurrent statements queue and at most `max_attempts` of them ever get one
    back - whatever their order, and whether or not one of them is right.

    Committed before returning, so the charge survives the 401 that usually follows
    (`async_get_db` does not commit on unwind) and the row lock is released before the
    comparison rather than held across the rest of the request.

    The digest is left in place by this statement, because it has to be returned: Postgres's
    `RETURNING` reads the new row, so nulling it here would hand back `NULL` on the last
    attempt. `spend_exhausted_code` clears it after a wrong guess instead.
    """
    code_hash = (
        await db.execute(
            update(AuthenticationRequest)
            .where(
                AuthenticationRequest.id == request_id,
                AuthenticationRequest.used_at.is_(None),
                AuthenticationRequest.code_hash.is_not(None),
                AuthenticationRequest.code_attempts < max_attempts,
            )
            .values(code_attempts=AuthenticationRequest.code_attempts + 1)
            .returning(AuthenticationRequest.code_hash)
        )
    ).scalar_one_or_none()
    await db.commit()
    return code_hash


async def spend_exhausted_code(db: AsyncSession, *, request_id: int, max_attempts: int) -> None:
    """Null a row's code digest once its attempts have run out, so `code_hash IS NULL` goes
    on meaning "spent". Not the cap - `charge_code_attempt` already refuses past it - but
    the reason a fast-path read can reject a spent code without charging anything.

    Nulling `code_hash` kills **the code and nothing else**. The link in the same email is
    untouched, on purpose: it carries 256 bits delivered to a mailbox and needs no
    protection from code guessing, whereas voiding it here would hand anyone who can
    reach this endpoint a way to cancel a sign-in they cannot complete - aimed squarely at
    the accounts whose only method is email.

    Left uncommitted: the caller's audit event for the wrong guess commits it.
    """
    await db.execute(
        update(AuthenticationRequest)
        .where(AuthenticationRequest.id == request_id, AuthenticationRequest.code_attempts >= max_attempts)
        .values(code_hash=None)
    )
