from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr


class AuthenticationRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    token_hash: str
    expires_at: datetime
    purpose: str = "sign_in"
    user_id: int | None = None


class AuthenticationRequestUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    used_at: datetime | None = None
    invalidated_at: datetime | None = None


class AuthenticationRequestRead(BaseModel):
    id: int
    email: EmailStr
    token_hash: str
    expires_at: datetime
    used_at: datetime | None
    invalidated_at: datetime | None
    purpose: str
    user_id: int | None
    created_at: datetime
