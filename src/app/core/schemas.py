import uuid as uuid_pkg
from datetime import datetime
from typing import ClassVar, Self

from pydantic import BaseModel, model_validator

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


class RejectsExplicitNulls(BaseModel):
    """Base for a PATCH body: refuses an explicit `null` for a field whose column is
    `NOT NULL`.

    Every field on an update schema is typed `T | None`, because that is how "omit it to
    leave it alone" is spelled in a PATCH body. But for a `NOT NULL` column an explicit
    `null` is a different thing entirely: it means nothing the database will accept, and
    it survives `model_dump(exclude_unset=True)` all the way into the UPDATE. Refusing it
    here means the caller gets a 422 naming the field, instead of an `IntegrityError`
    surfacing as a 500 (or, on the dive routes, as a foreign-key message for a not-null
    problem) - and the routes downstream can trust that anything present is really a
    value. See DECISIONS.md.

    Subclasses list their own `NOT NULL` columns in `NON_NULLABLE_FIELDS`. Anything left
    off it keeps accepting a null, which is a real operation: clearing `max_depth` back
    to "not recorded", or detaching a dive from its trip.
    """

    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ()

    @model_validator(mode="after")
    def _reject_explicit_nulls(self) -> Self:
        """`model_fields_set` is what separates "sent as null" from "not sent" - the same
        distinction `DiveUpdate.trip_uuid` relies on for the opposite purpose.
        """
        nulled = [
            name for name in self.NON_NULLABLE_FIELDS if name in self.model_fields_set and getattr(self, name) is None
        ]
        if nulled:
            fields = ", ".join(nulled)
            raise ValueError(f"{fields} cannot be null; omit the field to leave it unchanged")
        return self


# -------------- token --------------
class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    """Decoded subject of an access/refresh token.

    The subject is the user's immutable public `uuid`, never their username or email.
    Both of those are editable and, once released, immediately claimable by someone
    else - so a token naming one is a session that can silently re-point at a
    different account. See `services.auth_service.issue_tokens` and DECISIONS.md.
    """

    user_uuid: uuid_pkg.UUID


# -------------- google auth --------------
class GoogleUserInfo(BaseModel):
    """Account info extracted from a verified Google ID token."""

    google_id: str
    email: str
    name: str
    avatar: str | None = None


# -------------- onboarding (temporary, pre-account session) --------------
class OnboardingTokenData(BaseModel):
    """Decoded payload of a short-lived onboarding JWT (see `create_onboarding_token`/
    `verify_onboarding_token` in `core.security`), issued once an email or Google
    identity has been verified but no `User` row exists for it yet. Never persisted -
    it only ever lives inside the signed token itself, carried by the frontend from
    `/auth/email/verify` or `/auth/google` to `/auth/complete`.
    """

    email: str
    provider: str
    provider_user_id: str | None = None
    name: str | None = None
    avatar: str | None = None


class DiveFileTokenData(BaseModel):
    """Decoded payload of a dive-file token (see `create_dive_file_token`/
    `verify_dive_file_token` in `core.security`), minted by `POST /dive/parse` and
    presented again by `PUT /dive/{uuid}/file`.

    It attests one thing: this server successfully parsed *these exact bytes* for
    *this user*, recently. That is what lets the upload endpoint store a file without
    re-parsing it, and what ties a stored export to the dive whose form it pre-filled.
    """

    user_uuid: str
    sha256: str
    parser_key: str


class TokenBlacklistBase(BaseModel):
    token: str
    expires_at: datetime


class TokenBlacklistRead(TokenBlacklistBase):
    id: int


class TokenBlacklistCreate(TokenBlacklistBase):
    pass


class TokenBlacklistUpdate(TokenBlacklistBase):
    pass
