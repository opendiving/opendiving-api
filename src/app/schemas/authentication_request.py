import uuid as uuid_pkg
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr


class AuthenticationRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    token_hash: str
    expires_at: datetime
    # Only the sign-in flow sets this; an email change is confirmed in the new mailbox,
    # so there is no code to type. See `AuthenticationRequest.code_hash`.
    code_hash: str | None = None
    purpose: str = "sign_in"
    user_id: int | None = None


class AuthenticationRequestUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    used_at: datetime | None = None
    invalidated_at: datetime | None = None


class AuthenticationRequestRead(BaseModel):
    id: int
    uuid: uuid_pkg.UUID
    email: EmailStr
    token_hash: str
    expires_at: datetime
    code_hash: str | None
    code_attempts: int
    used_at: datetime | None
    invalidated_at: datetime | None
    purpose: str
    user_id: int | None
    created_at: datetime
