"""Schemas for the `invite_request` table, its anonymous endpoint and the operator's queue.

The response side is where the design shows. `POST /invite-requests` answers one frozen
message for every address, which is why `InviteRequestAccepted` carries a default rather
than a value any handler computes - the endpoint is anonymous, and a response that varied
would be an account-existence oracle for anyone who can reach the port.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from .invitation import MAX_ADDRESSES_PER_BATCH


class InviteRequestCreateInternal(BaseModel):
    """Server-composed only - `email` is lowercased by the route before it gets here."""

    model_config = ConfigDict(extra="forbid")

    email: str


class InviteRequestSubmission(BaseModel):
    """`POST /invite-requests`' body."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class InviteRequestAccepted(BaseModel):
    """The one answer this endpoint gives, whatever it did.

    A first request, a repeat, an address that already has an account and an address that
    was invited last week all get this - and the handler never queries the `user` table at
    all, so the guarantee is structural rather than a matter of shaping the response. The
    same discipline `EmailAuthRequestResponse` carries one endpoint over.
    """

    message: str = "Thanks - we'll email you if an invitation comes your way."


class AdminInviteRequestRead(BaseModel):
    """One row of the operator's queue.

    `has_account` is a join on `lower(User.email)` - *this* route may look, because its
    caller is the operator rather than an anonymous stranger. It exists because a request
    from an address that already has an account is stored like any other (the anonymous
    endpoint structurally cannot know), and this flag is how the operator spots one and
    removes it instead of inviting somebody who is already here.
    """

    email: EmailStr
    created_at: datetime
    has_account: bool


class AdminInviteRequestDeleteRequest(BaseModel):
    """`DELETE /admin/invite-requests`' body: addresses to drop from the queue.

    Body-keyed rather than a `{uuid}` route, because the row has no public identifier -
    the address is the only thing anyone knows about a request. That also keeps this route
    out of the ownership registry: there is no addressed resource whose owner could be
    checked.
    """

    model_config = ConfigDict(extra="forbid")

    emails: list[EmailStr] = Field(min_length=1, max_length=MAX_ADDRESSES_PER_BATCH)


class AdminInviteRequestDeleteResponse(BaseModel):
    """How many rows actually went, which is not always how many were asked for - an
    address the sweep already took, or that somebody invited a moment ago, is simply not
    there any more."""

    removed: int
