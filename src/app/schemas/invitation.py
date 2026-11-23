"""Schemas for the `invitation` table and the operator's batch over it.

`InvitationReadInternal` mirrors the table for server-side lookups. No route answers a row:
the batch reports an outcome per address. An invitation by address is an allow-list entry,
not a token (see `models/invitation.py`), and the one invitation secret there is, a member's
connect-link token, lives on its own table and is in none of these.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ..core.schemas import AccountEmail, PublicUUIDSchema


class InvitationCreateInternal(BaseModel):
    """Server-composed only. `email` is lowercased by the route before it reaches here, and
    `user_id` is the calling account - neither is ever taken from a body as-is.
    """

    model_config = ConfigDict(extra="forbid")

    email: str
    user_id: int
    # Only the admin batch route ever sets it, and only when it deleted a pending request
    # for the address - see `Invitation.from_invite_request`.
    from_invite_request: bool = False


class InvitationUpdate(BaseModel):
    """Only ever the two stamps, and never from a request body.

    `accepted_at` is written by the account-creating transaction, through a hand-written
    statement in `crud.crud_invitations`; `revoked_at` is written by nothing, and the gate
    still reads a stamp a row already carries. This exists to give the FastCRUD alias its
    update types rather than to be filled in by anybody.
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

    emails: list[AccountEmail] = Field(
        min_length=1, max_length=MAX_ADDRESSES_PER_BATCH, description="Addresses to invite, at most 100"
    )


class AdminInvitationOutcome(BaseModel):
    """What became of one address in the batch.

    Per-address rather than a 5xx on the first failure, so a partial SMTP outage is visible
    as *which* addresses got through rather than as an error that hides them. `mail_failed`
    is the one that matters: the invitation row is committed by then and the address is
    admitted, so the operator's job is to tell them another way, not to invite again.
    """

    email: AccountEmail
    outcome: str


class AdminInvitationBatchResponse(BaseModel):
    results: list[AdminInvitationOutcome]
