"""Schemas for the `connect_link` table and the routes over it."""

import uuid as uuid_pkg
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field

_AVATAR_DESCRIPTION = (
    "The owner's picture version, passed as `v` to `GET /connect-link/{token}/avatar`; null without one"
)


class ConnectLinkRead(BaseModel):
    """The caller's own link: the token their `/connect/<token>` address carries, and when it
    was made. A client composes the address from its own origin, as it does a check-in
    link's."""

    token: str
    created_at: datetime


class ConnectLinkRemovedResponse(BaseModel):
    """`DELETE /user/connect-link`'s answer."""

    message: str = "Connect link removed"


class ConnectLinkOwner(BaseModel):
    """What `GET /connect-link/{token}` tells whoever opened the link, signed in or not: whose
    it is, by their name and picture."""

    name: str
    avatar_sha256: Annotated[str | None, Field(default=None, description=_AVATAR_DESCRIPTION)]


class ConnectLinkOwnerForYou(ConnectLinkOwner):
    """What `GET /connect-link/{token}/owner` tells a signed-in caller: the owner's username
    besides, and where the caller already stands with them."""

    username: str
    is_yours: Annotated[bool, Field(description="Whether the link is the caller's own")]
    person_uuid: Annotated[
        uuid_pkg.UUID | None,
        Field(default=None, description="The caller's person connected to the link's owner, or null"),
    ]
