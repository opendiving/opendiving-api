"""The bodies of the invite-link routes, held for the clients that still call them until those
routes go; they serve the `connect_link` table."""

from datetime import datetime

from pydantic import BaseModel


class InviteLinkRead(BaseModel):
    """The caller's own link: the token, and when it was made."""

    token: str
    created_at: datetime


class InviteLinkRemovedResponse(BaseModel):
    """`DELETE /user/invite-link`'s answer, in `InvitationRevokedResponse`'s shape."""

    message: str = "Invite link removed"


class InviteLinkInviter(BaseModel):
    """What `GET /invite-link/{token}` tells whoever opened the link: whose it is, by name."""

    inviter_name: str
