from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class DownloadTicketCreate(BaseModel):
    """`POST /user/download-ticket`'s body: the download the ticket is for."""

    model_config = ConfigDict(extra="forbid")

    path: Annotated[
        str,
        Field(
            min_length=2,
            max_length=512,
            pattern=r"^/[^?#]*$",
            description="The download's path under `/api/v1`, such as `/export/archive`, with no query string",
        ),
    ]


class DownloadTicketMinted(BaseModel):
    """A ticket to append to that path as `?ticket=`."""

    ticket: str
    expires_in: int
