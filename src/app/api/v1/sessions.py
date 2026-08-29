"""Listing and revoking the devices signed in to an account - every route here is `/user/*`.

The read-and-revoke half of server-side sessions. The half that *creates* the rows is in
`services.auth_service.issue_tokens`, beside the token mint it is inseparable from, exactly
as passkey registration and passkey sign-in are split between `api.v1.passkeys` and
`api.v1.auth`.

**Revoking a session ends its refresh, not its access token.** A revoked row cannot rotate
anything, so the device is signed out at its next `/auth/refresh`; the access token it is
already holding stays valid for up to `ACCESS_TOKEN_EXPIRE_MINUTES`. That is the shape
`DELETE /user` has always had - other devices go inert at the next refresh rather than
instantly - and the alternative, a session-liveness query inside `get_current_user`, was
rejected for buying at most half an hour of promptness at the cost of a database read on
every authenticated request in the app.
"""

import uuid as uuid_pkg
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import current_session_uuid, fetch_owned_or_raise, get_current_user
from ...core.db.database import async_get_db
from ...core.utils.request_context import RequestContext
from ...crud.crud_auth_audit_events import record_auth_event
from ...crud.crud_user_sessions import (
    MAX_LIVE_SESSIONS_PER_USER,
    crud_user_sessions,
    live_sessions_for_user,
    revoke_other_sessions,
    revoke_session,
)
from ...schemas.auth_audit_event import AuthEventType
from ...schemas.user_session import (
    SessionRevokedResponse,
    SessionsRevokedResponse,
    UserSessionRead,
    UserSessionReadInternal,
    to_public_session,
)

router = APIRouter(tags=["sessions"])

_NOT_FOUND = "Session not found"

# Deliberately *above* `MAX_LIVE_SESSIONS_PER_USER` rather than equal to it, for the reason
# `passkeys.py` records for its own `_LIST_LIMIT`: a row nobody can see is a row nobody can
# revoke. Here the way an account can exceed the cap is a race - two sign-ins can both count
# live rows before either inserts - so the margin only has to cover concurrency, but it must
# exist, because a list that truncated would hide exactly the extra session.
_LIST_LIMIT = MAX_LIVE_SESSIONS_PER_USER * 2


@router.get("/user/sessions", response_model=list[UserSessionRead])
async def read_sessions(
    current_user: Annotated[dict, Depends(get_current_user)],
    session_uuid: Annotated[uuid_pkg.UUID | None, Depends(current_session_uuid)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> list[UserSessionRead]:
    """Every device currently signed in to the caller's account, most recently used first.

    Unpaginated, like `GET /user/passkeys`, and **not Redis-cached - for a reason of its
    own rather than that one's.** Passkeys skip the cache because there is nothing worth
    caching; this response cannot be cached correctly at all. It carries `current` per row,
    resolved from the requesting token's `sid`, so it varies by credential rather than by
    user - and every cache key in this app is user-scoped, which is what pattern
    invalidation depends on. A cached entry would hand one device's "This device" marker to
    another, which is a wrong answer rather than a stale one.

    Revoked and expired rows are absent: the list is what can still authenticate something,
    which is the same predicate `/auth/refresh` checks.
    """
    rows = await live_sessions_for_user(db, user_id=current_user["id"], limit=_LIST_LIMIT)
    return [to_public_session(row, current_session_uuid=session_uuid) for row in rows]


@router.delete("/user/session/{uuid}", response_model=SessionRevokedResponse)
async def erase_session(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    session_uuid: Annotated[uuid_pkg.UUID | None, Depends(current_session_uuid)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> SessionRevokedResponse:
    """Sign one other device out.

    404 unless the caller owns it, exactly as for a session that does not exist - the
    ownership contract every keyed route here honours.

    **409 for the caller's own session, which is the one deliberate departure from
    "revoke = remove the row".** Ending your own session is what `POST /auth/logout` is,
    and it has to also clear the refresh cookie and blacklist the presented pair; a revoke
    that left the browser holding a live access token and no cookie would be a worse
    logout than the one that already exists. So the current row is marked in the list and
    carries no revoke control, and this status is the backstop rather than the UX.

    A second revoke of an already-revoked session succeeds and changes nothing, unlike the
    hard-deleting resources whose second `DELETE` is a 404: the row is not what is being
    removed, and the caller already owns it.

    An access token minted before this feature names no session, so for its remaining
    minutes the 409 cannot fire and such a caller *can* revoke the row they are on. That is
    deliberate rather than a hole: with no `sid` there is nothing to compare against, the
    list marks nothing "This device" either, and the only consequence is the 401 at their
    next refresh - which a token that old is heading for regardless.
    """
    await fetch_owned_or_raise(
        db=db,
        crud=crud_user_sessions,
        uuid=uuid,
        current_user=current_user,
        schema=UserSessionReadInternal,
        not_found_message=_NOT_FOUND,
    )

    if session_uuid is not None and uuid == session_uuid:
        # A raw `HTTPException` for the reason `passkey_service._at_the_cap` gives:
        # `core/exceptions/http_exceptions.py` has no class for 409.
        raise HTTPException(status_code=409, detail="This is the session you are signed in with. Sign out instead.")

    await revoke_session(db, session_uuid=uuid)
    await record_auth_event(
        db,
        event_type=AuthEventType.SESSION_REVOKED,
        context=RequestContext.from_request(request),
        user_id=current_user["id"],
    )

    return SessionRevokedResponse(message="Session revoked")


@router.delete("/user/sessions", response_model=SessionsRevokedResponse)
async def erase_other_sessions(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    session_uuid: Annotated[uuid_pkg.UUID | None, Depends(current_session_uuid)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> SessionsRevokedResponse:
    """Sign every *other* device out, and report how many that was.

    The count is the payload rather than decoration: the client confirms before it sends
    this, so the number of sessions actually ended cannot come from the dialog and has to
    come from here.

    The caller's own session is spared, which is what makes this "sign out other sessions"
    rather than a global logout. An access token minted before sessions existed names no
    session to spare, so from such a token this signs everything out including the caller -
    the honest answer for the one access-token lifetime in which it can happen.

    One event for the whole action, not one per row. The rows revoked here are the
    *subject* of the event, and a per-row row would turn one deliberate act into ninety-nine
    entries.
    """
    revoked = await revoke_other_sessions(db, user_id=current_user["id"], except_uuid=session_uuid)
    await record_auth_event(
        db,
        event_type=AuthEventType.OTHER_SESSIONS_REVOKED,
        context=RequestContext.from_request(request),
        user_id=current_user["id"],
    )

    return SessionsRevokedResponse(message="Other sessions revoked", revoked=revoked)
