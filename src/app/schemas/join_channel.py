from pydantic import BaseModel


class JoinChannelRead(BaseModel):
    """One configured join channel: the slug its link carries, and the name it is shown by."""

    slug: str
    label: str
