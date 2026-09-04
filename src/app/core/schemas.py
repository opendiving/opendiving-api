import uuid as uuid_pkg
from datetime import date, datetime
from typing import ClassVar, Self

from pydantic import BaseModel, model_validator

# Free-text "notes" fields (Dive, DiveSite, Trip) are stored as unbounded `Text`
# columns in Postgres (VARCHAR(n) and TEXT perform identically there), so this
# limit exists purely to keep payloads sane - not because of any storage constraint.
NOTES_MAX_LENGTH = 10_000

DATE_RANGE_MESSAGE = "end_date must be on or after start_date"


def validate_date_range(start_date: date | None, end_date: date | None) -> None:
    """The one place a `start_date`/`end_date` pair's ordering is decided.

    Shared by `TripBase`/`TripUpdate` and `CourseBase`/`CourseUpdate`, and public because
    `patch_trip`/`patch_course` each have to re-run it on a merged stored+incoming pair -
    the one case an update schema cannot see, since a PATCH may carry either date alone.
    Only `course` has a CHECK constraint underneath it; on `trip` this is the whole
    enforcement.

    Lives here rather than beside either resource so the two cannot drift apart into two
    spellings of the same rule.
    """
    if start_date is not None and end_date is not None and end_date < start_date:
        raise ValueError(DATE_RANGE_MESSAGE)


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

    # The `user_session` row this token belongs to, carried unchanged across every
    # rotation. `None` for a token minted before sessions existed - such a token's next
    # refresh 401s and the diver signs in again, which is a one-time global sign-out on
    # upgrade and the whole of the compatibility story (there is no shim).
    #
    # A field rather than a second return type: `verify_token` runs on every authenticated
    # request, and widening its return into a discriminated result is the change
    # `DECISIONS.md` §"A reused refresh token is a `WARNING`" rejected for reshaping the
    # hottest path in the app. Reading one more claim off a payload already decoded costs
    # nothing, and `get_current_user` simply does not look at it.
    session_uuid: uuid_pkg.UUID | None = None


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


class LogbookImportTokenData(BaseModel):
    """Decoded payload of a logbook-import token (see `create_logbook_import_token`/
    `verify_logbook_import_token` in `core.security`), minted by
    `POST /import/divejson/preview` and presented again by `POST /import/divejson`.

    The same shape and the same modest claim as `DiveFileTokenData` above, minus the
    parser: this server read *these exact bytes* for *this user* and showed them a report
    of what importing them would do. It carries no authority - apply re-plans the whole
    document from scratch inside its own transaction - so what it actually buys is that
    the report a diver approved describes the file they then applied.
    """

    user_uuid: str
    sha256: str


class TokenBlacklistBase(BaseModel):
    token: str
    expires_at: datetime
    revoked_at: datetime


class TokenBlacklistRead(TokenBlacklistBase):
    id: int


class TokenBlacklistCreate(TokenBlacklistBase):
    pass


class TokenBlacklistUpdate(TokenBlacklistBase):
    pass
