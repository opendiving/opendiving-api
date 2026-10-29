from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from ..core.schemas import AccountEmail


class EmailChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_email: Annotated[AccountEmail, Field(description="Address to change the account's email to")]


class EmailChangeRequestResponse(BaseModel):
    """Always the same generic message, regardless of whether `new_email` already
    belongs to another account - see `POST /user/email-change/request`.
    """

    message: str = "Check your new email address to confirm the change."


class EmailChangeVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: Annotated[str, Field(description="Token from the confirmation link sent to the new address")]


class EmailChangeVerifyResponse(BaseModel):
    message: str = "Your email address has been updated."
    email: str
