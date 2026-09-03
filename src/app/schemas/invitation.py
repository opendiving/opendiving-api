"""Schemas for the `invitation` table and the routes over it.

The internal/public split is `user_session`'s: `InvitationReadInternal` mirrors the table
for server-side lookups - including the ownership check `fetch_owned_or_raise` runs on
`DELETE /user/invitation/{uuid}`, which needs the `user_id` its `OwnedRow` protocol asks
for - and `InvitationRead` is what an inviter sees. There is no secret to withhold on the
way out: an invitation is an allow-list entry, not a token (see `models/invitation.py`).
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from ..core.schemas import PublicUUIDSchema


class InvitationCreateInternal(BaseModel):
    """Server-composed only. `email` is lowercased by the route before it reaches here, and
    `user_id` is the calling account - neither is ever taken from a body as-is.
    """

    model_config = ConfigDict(extra="forbid")

    email: str
    user_id: int


class InvitationUpdate(BaseModel):
    """Only ever the two stamps, and never from a request body.

    `accepted_at` is written by the account-creating transaction and `revoked_at` by the
    inviter's revoke; both go through hand-written statements in `crud.crud_invitations`,
    so this exists to give the FastCRUD alias its update types rather than to be filled in
    by anybody.
    """

    model_config = ConfigDict(extra="forbid")

    accepted_at: datetime | None = None
    revoked_at: datetime | None = None


class InvitationReadInternal(PublicUUIDSchema):
    """Mirrors the table, integer keys included - server-side lookups only."""

    id: int
    user_id: int
    email: str
    created_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None


class InvitationRead(PublicUUIDSchema):
    """One row of `GET /user/invitations`.

    The address is here because the inviter typed it and needs to recognise the row; the
    two stamps are what the card renders as pending / accepted on a date / revoked. There
    is no "status" field: three nullable timestamps say more than one enum would, and the
    client already has to render the date.
    """

    email: EmailStr
    created_at: datetime
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None


class InvitationCreateRequest(BaseModel):
    """`POST /user/invitations`' body: the address to invite, and nothing else.

    `EmailStr` rather than a bare string, matching `EmailAuthRequest` - a malformed address
    is a 422 naming the field rather than a row nobody can ever accept.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class InvitationRevokedResponse(BaseModel):
    """`DELETE /user/invitation/{uuid}`'s answer. A message rather than an empty 204, so a
    client can show one - the same shape `SessionRevokedResponse` takes for the revoke
    beside it."""

    message: str = "Invitation revoked"


# The cap on `POST /admin/invitations`. A hundred is `DEFAULT_MAX_ITEMS_PER_PAGE`'s number,
# chosen for the same reason: it is the size of one screenful of queue the operator has
# just selected, and the route sends the emails inline and sequentially, so an unbounded
# body would be an unbounded request.
MAX_ADDRESSES_PER_BATCH = 100


class AdminInvitationBatchRequest(BaseModel):
    """`POST /admin/invitations`' body: the addresses to invite.

    Addresses that never requested are accepted too - the operator inviting a colleague is
    the same act, and a request row is optional context rather than a precondition.
    """

    model_config = ConfigDict(extra="forbid")

    emails: list[EmailStr] = Field(min_length=1, max_length=MAX_ADDRESSES_PER_BATCH)


class AdminInvitationOutcome(BaseModel):
    """What became of one address in the batch.

    Per-address rather than a 5xx on the first failure, so a partial SMTP outage is visible
    as *which* addresses got through rather than as an error that hides them. `mail_failed`
    is the one that matters: the invitation row is committed by then and the address is
    admitted, so the operator's job is to tell them another way, not to invite again.
    """

    email: EmailStr
    outcome: str


class AdminInvitationBatchResponse(BaseModel):
    results: list[AdminInvitationOutcome]
