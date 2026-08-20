import uuid as uuid_pkg
from datetime import UTC, datetime
from typing import cast

from fastcrud import FastCRUD
from sqlalchemy import CursorResult, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.webauthn_credential import WebauthnCredential
from ..schemas.webauthn_credential import (
    WebauthnCredentialCreateInternal,
    WebauthnCredentialReadInternal,
    WebauthnCredentialUpdate,
    WebauthnCredentialUpdateInternal,
)

CRUDWebauthnCredential = FastCRUD[
    WebauthnCredential,
    WebauthnCredentialCreateInternal,
    WebauthnCredentialUpdate,
    WebauthnCredentialUpdateInternal,
    WebauthnCredentialUpdate,
    WebauthnCredentialReadInternal,
]
crud_webauthn_credentials = CRUDWebauthnCredential(WebauthnCredential)


async def record_assertion(
    db: AsyncSession,
    *,
    credential_uuid: uuid_pkg.UUID,
    sign_count: int,
    backed_up: bool,
    expected_sign_count: int,
) -> bool:
    """Stamp a verified assertion onto the credential row, returning whether *this*
    caller is the one that did it. `False` means a concurrent assertion got there first.

    Hand-written Core with `sign_count` in the `WHERE`, for the same reason
    `claim_authentication_request` is: the counter check that makes a replayed assertion
    fail happens against the value read *before* verification, and a read plus a later
    write is two statements two concurrent submissions of the same assertion can both walk
    through. Conditioning the UPDATE on the count still being what verification was run
    against collapses that back into one, so exactly one of two racing replays advances the
    counter - and FastCRUD's filtered `update` cannot express it (it issues a `count()`
    before the UPDATE and discards the statement's own `rowcount`; see *"A filter on a
    FastCRUD `update` is a `count()`, not an atomic condition"* in `DECISIONS.md`).

    The loser is not an error the *user* sees anything about: both requests presented the
    same valid assertion, so whichever lost simply must not also mint a session.
    """
    result = cast(
        CursorResult,
        await db.execute(
            update(WebauthnCredential)
            .where(
                WebauthnCredential.uuid == credential_uuid,
                WebauthnCredential.sign_count == expected_sign_count,
            )
            .values(sign_count=sign_count, backed_up=backed_up, last_used_at=datetime.now(UTC))
        ),
    )
    # Read before committing: the count belongs to the statement, not to the transaction.
    recorded = result.rowcount > 0
    await db.commit()
    return recorded
