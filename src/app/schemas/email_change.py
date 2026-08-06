from pydantic import BaseModel, ConfigDict, EmailStr


class EmailChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_email: EmailStr


class EmailChangeRequestResponse(BaseModel):
    """Always the same generic message, regardless of whether `new_email` already
    belongs to another account - see `POST /user/{uuid}/email-change/request`.
    """

    message: str = "Check your new email address to confirm the change."


class EmailChangeVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str


class EmailChangeVerifyResponse(BaseModel):
    message: str = "Your email address has been updated."
    email: str
