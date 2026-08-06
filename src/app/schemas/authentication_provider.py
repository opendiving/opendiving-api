from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AuthenticationProviderBase(BaseModel):
    user_id: int
    provider: str
    provider_user_id: str | None = None


class AuthenticationProviderCreate(AuthenticationProviderBase):
    model_config = ConfigDict(extra="forbid")


class AuthenticationProviderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_user_id: str | None = None


class AuthenticationProviderRead(AuthenticationProviderBase):
    id: int
    created_at: datetime
