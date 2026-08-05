import uuid as uuid_pkg
from datetime import datetime

from pydantic import BaseModel

# Free-text "notes" fields (Dive, DiveSite, Trip) are stored as unbounded `Text`
# columns in Postgres (VARCHAR(n) and TEXT perform identically there), so this
# limit exists purely to keep payloads sane - not because of any storage constraint.
NOTES_MAX_LENGTH = 10_000


class HealthCheck(BaseModel):
    name: str
    version: str
    description: str


# -------------- mixins --------------
class PublicUUIDSchema(BaseModel):
    """Adds the opaque, uuid7-based `uuid` field exposed as a resource's public
    identifier (e.g. in URLs) instead of the internal sequential `id`. Mirrors the
    SQLAlchemy-side `PublicUUIDMixin` in `core.db.models`.
    """

    uuid: uuid_pkg.UUID


# -------------- token --------------
class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    username_or_email: str


# -------------- google auth --------------
class GoogleAuthRequest(BaseModel):
    # The ID token (a JWT) returned to the frontend by Google Identity Services
    # after the user signs in with their Google account.
    credential: str


class GoogleUserInfo(BaseModel):
    """Account info extracted from a verified Google ID token."""

    google_id: str
    email: str
    name: str


class TokenBlacklistBase(BaseModel):
    token: str
    expires_at: datetime


class TokenBlacklistRead(TokenBlacklistBase):
    id: int


class TokenBlacklistCreate(TokenBlacklistBase):
    pass


class TokenBlacklistUpdate(TokenBlacklistBase):
    pass
