"""Schemas for the `invite_link` table and the routes over it."""

from datetime import datetime

from pydantic import BaseModel


class InviteLinkRead(BaseModel):
    """The caller's own link: the token their `/join?via=` address carries, and when it was
    made. The web composes the address from its own origin, as it does a check-in link's."""

    token: str
    created_at: datetime


class InviteLinkRemovedResponse(BaseModel):
    """`DELETE /user/invite-link`'s answer, in `InvitationRevokedResponse`'s shape."""

    message: str = "Invite link removed"


class InviteLinkInviter(BaseModel):
    """What `GET /invite-link/{token}` tells whoever opened the link: whose it is, by the name
    an invitation email already shows its invitee."""

    inviter_name: str
