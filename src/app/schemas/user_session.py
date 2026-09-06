"""Schemas for the `user_session` table and the three routes over it.

The internal/public split is `webauthn_credential`'s: `UserSessionReadInternal` mirrors the
table for server-side lookups, `UserSessionRead` is what a diver sees. Unlike a credential
there is no secret to drop on the way out - a session row holds nothing token-derived, by
construction - so what the public shape actually withholds is the internal ids, and what it
*adds* is `current`, which the row cannot know.
"""

import uuid as uuid_pkg
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from ..core.schemas import PublicUUIDSchema


class UserSessionCreateInternal(BaseModel):
    """Server-composed only. Every field comes from the request that minted the tokens;
    nothing here is ever taken from a body.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: int
    expires_at: datetime
    ip: str
    user_agent: str


class UserSessionReadInternal(PublicUUIDSchema):
    """Mirrors the table, integer key included - server-side lookups only, including the
    ownership check `fetch_owned_or_raise` runs on `DELETE /user/session/{uuid}`.
    """

    id: int
    user_id: int
    expires_at: datetime
    ip: str
    user_agent: str
    created_at: datetime
    last_used_at: datetime
    revoked_at: datetime | None


class UserSessionRead(PublicUUIDSchema):
    """What `GET /user/sessions` returns for one row.

    `user_agent` is the raw header rather than a label: the web client derives "Chrome on
    macOS" with the parser it already ships for passkeys, so one browser cannot be named
    two different things on one settings page.

    `expires_at` is deliberately **not** here. It is a sliding inactivity window driven by
    `REFRESH_TOKEN_EXPIRE_DAYS`, and publishing the computed date would restate a setting
    as a fact - the privacy copy says "about a week" precisely because the number is the
    operator's (`DECISIONS.md` §"The operator docs carry the consent duty").

    `revoked_at` is absent for a simpler reason: the list only ever contains live rows.
    """

    created_at: datetime
    last_used_at: datetime
    ip: str
    user_agent: str

    # Resolved from the requesting access token's `sid`, not from the row - which is why
    # this response is never cached. See `api.v1.sessions.read_sessions`.
    current: bool


class SessionsRevokedResponse(BaseModel):
    """`DELETE /user/sessions`. The count is the payload: the client confirms before the
    request, so the number of sessions actually ended can only come from the response.
    """

    message: str
    revoked: int


class SessionRevokedResponse(BaseModel):
    """`DELETE /user/session/{uuid}`."""

    message: str


def to_public_session(row: UserSessionReadInternal, *, current_session_uuid: uuid_pkg.UUID | None) -> UserSessionRead:
    """The one place a stored row becomes the diver's view of it.

    `current_session_uuid` is `None` only where nothing identified the requesting device, and
    then nothing is marked current rather than something being marked wrongly. No route hands
    it `None` any more - `get_current_user` refuses a token carrying no `sid` - and the
    branch stays because "I cannot tell" is the right answer to keep available, not a state
    to assume away.
    """
    return UserSessionRead(
        uuid=row.uuid,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        ip=row.ip,
        user_agent=row.user_agent,
        current=current_session_uuid is not None and row.uuid == current_session_uuid,
    )
