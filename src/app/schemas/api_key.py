"""Schemas for the `api_key` table and its management routes.

The `webauthn_credential` split: `ApiKeyReadInternal` carries the integer keys for
server-side lookups, `ApiKeyRead` is what a diver sees, and neither carries the hash. The
secret itself appears in one schema, `ApiKeyCreated`, which only the create route returns.
"""

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..authorization.scopes import validate_scopes
from ..core.schemas import PublicUUIDSchema
from .webauthn_credential import NAME_MAX_LENGTH


class ApiKeyScope(StrEnum):
    """Scoped content access, or a legacy read/write key's unchanged access mode."""

    READ = "read"
    WRITE = "write"
    SCOPED = "scoped"


class ApiKeyLifetime(StrEnum):
    """How long a key lasts, chosen at creation. A vocabulary rather than a date, so the
    server computes `expires_at` and a client never sends one."""

    THIRTY_DAYS = "30d"
    NINETY_DAYS = "90d"
    ONE_YEAR = "1y"
    NEVER = "never"

    def expires_at(self, now: datetime) -> datetime | None:
        days = {"30d": 30, "90d": 90, "1y": 365}.get(self.value)
        return None if days is None else now + timedelta(days=days)


class ApiKeyCreate(BaseModel):
    """`POST /user/api-keys`'s body."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[
        str,
        Field(
            min_length=1,
            max_length=NAME_MAX_LENGTH,
            examples=["laptop"],
            description="Label that identifies the key in the key list",
        ),
    ]
    scope: Annotated[
        ApiKeyScope,
        Field(description="scoped grants only the listed permissions; read and write preserve legacy access"),
    ]
    lifetime: Annotated[ApiKeyLifetime, Field(description="How long the key stays valid: 30d, 90d, 1y or never")]
    scopes: list[str] | None = Field(
        default=None, description="Explicit permissions for a scoped key; omitted for legacy read/write keys"
    )

    @model_validator(mode="after")
    def check_permissions(self) -> ApiKeyCreate:
        if self.scope == ApiKeyScope.SCOPED:
            if self.scopes is None or not validate_scopes(self.scopes):
                raise ValueError("Choose at least one supported permission, without duplicates.")
        elif self.scopes is not None:
            raise ValueError("Detailed permissions require scope=scoped.")
        return self


class ApiKeyCreateInternal(BaseModel):
    """Server-composed only: the prefix and the hash come from `generate_api_key`, the expiry
    from the lifetime, and `name` and `scope` are what the diver chose."""

    model_config = ConfigDict(extra="forbid")

    user_id: int
    name: str
    prefix: str
    token_hash: str
    scope: ApiKeyScope
    scopes: list[str] | None = None
    expires_at: datetime | None


class ApiKeyReadInternal(PublicUUIDSchema):
    """The row with its integer keys, for server-side lookups - the ownership check on
    `DELETE /user/api-key/{uuid}` among them. Without the hash, which nothing reads back."""

    id: int
    user_id: int
    name: str
    prefix: str
    scope: ApiKeyScope
    scopes: list[str] | None = None
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None


class ApiKeyRead(PublicUUIDSchema):
    """What `GET /user/api-keys` returns per row: never the secret, the hash or the ids.

    `expires_at` is null for a key that never expires, and an expired key is listed until it
    is revoked. `last_used_at` is stamped at most once per `API_KEY_LAST_USED_GRANULARITY`,
    so it is accurate to within that.
    """

    name: str
    prefix: str
    scope: ApiKeyScope
    scopes: list[str] | None = None
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None


class ApiKeyCreated(ApiKeyRead):
    """The one response that carries the secret. Only its hash is kept, so it cannot be shown
    again."""

    secret: str


class ApiKeyRevokedResponse(BaseModel):
    message: str = "API key revoked"
