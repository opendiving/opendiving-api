from datetime import datetime
from enum import StrEnum
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from ..core.schemas import PublicUUIDSchema, RejectsExplicitNulls


class UnitSystem(StrEnum):
    """Which measurement system a diver reads and types in.

    Two whole systems rather than a choice per dimension (depth, pressure, weight,
    ...): the two real camps are m/C/bar/kg/L and ft/F/psi/lb/cuft, and one toggle
    covers both. Per-dimension granularity stays available as a later additive change.

    Nothing the API serves is converted - see DECISIONS.md's *"Measurements are metric
    in the database and on the wire; `units` is who's looking"*. This is the single
    source of truth for the vocabulary, and like `GearType` it is deliberately not
    mirrored by a DB `CHECK` constraint.
    """

    METRIC = "metric"
    IMPERIAL = "imperial"


class UserBase(BaseModel):
    name: Annotated[str, Field(min_length=2, max_length=30, examples=["User Userson"])]
    username: Annotated[str, Field(min_length=2, max_length=20, pattern=r"^[a-z0-9]+$", examples=["userson"])]
    email: Annotated[EmailStr, Field(examples=["user.userson@example.com"])]


class UserRead(PublicUUIDSchema):
    """Public representation of a user, keyed by their opaque `uuid` rather than the
    sequential internal `id` (which is never exposed over the API).
    """

    name: Annotated[str, Field(min_length=2, max_length=30, examples=["User Userson"])]
    username: Annotated[str, Field(min_length=2, max_length=20, pattern=r"^[a-z0-9]+$", examples=["userson"])]
    email: Annotated[EmailStr, Field(examples=["user.userson@example.com"])]
    # Null means this diver has no picture and the clients draw initials. Non-null is both
    # "there is one" and the version token to append to `GET /user/avatar` as `?v=`, so a
    # replaced avatar lands on a fresh URL and an unchanged one is never re-downloaded.
    # There is no URL here on purpose: the bytes need a bearer token, so the clients fetch
    # them through their API client rather than putting a src on an `<img>`.
    avatar_sha256: str | None = None
    # Feeds the settings page's gear-reminder toggle. The `= True` is what `/openapi.json`
    # publishes as the field's default; it is *not* a fallback for a database missing the
    # column, which is what this said for a while. `get_current_user` selects every mapped
    # column, so such a database raises `UndefinedColumn` and the request 500s before
    # Pydantic sees a row at all (see DECISIONS.md).
    gear_service_emails: bool = True
    # Feeds the settings page's units toggle, and every measurement the web app renders.
    # Same note as its neighbour above on what the default is and isn't for.
    units: UnitSystem = UnitSystem.METRIC
    # The caller's own record of whether they are this instance's operator, so a client can
    # decide whether to offer the operator's surface at all. Not a disclosure about anybody
    # else: `GET /user` returns the caller's row and no route returns another account's.
    # False for every account but the first one on an empty instance (see
    # `UserBootstrapCreateInternal`) and any promoted by hand.
    is_superuser: bool = False


class UserReadInternal(UserRead):
    """Adds the internal sequential `id`, for server-side lookups only - never returned
    directly over the API (use `UserRead` for that).
    """

    id: int


class UserCreateInternal(UserBase):
    """The only way *self-service registration* creates a `User` row - from a completed
    profile (`POST /auth/complete`), never directly from a signup form. There's no password
    field anywhere: identity is proven up front by the email-magic-link or Google flow, and
    the resulting authentication method is recorded separately (see
    `AuthenticationProviderCreate`), not on the user row itself.

    "Self-service" is the qualifier, not decoration: `scripts/create_first_superuser.py`
    and the admin panel's `User` view also write rows, and both bypass the registration
    gate (`services.registration_gate`) by construction. On an `invite`-mode instance the
    completion route creates a row only for an address that holds a live invitation, or for
    the very first account on an empty instance - which is `UserBootstrapCreateInternal`
    below rather than this schema.
    """

    # Set together or not at all, and only by the Google import in `POST /auth/complete`
    # (`services.user_avatars.import_google_avatar`), which has already written the blob by
    # the time the row is created - the write-file-then-commit-row ordering. An email
    # sign-up has no picture to import and starts with initials.
    avatar_storage_key: str | None = None
    avatar_sha256: str | None = None


class UserBootstrapCreateInternal(UserCreateInternal):
    """The first account on an empty instance, which is the operator's.

    A subclass rather than a defaulted field on `UserCreateInternal`, because that schema
    is also the admin panel's `create_schema` (`admin/views.py`) and a checkbox that grants
    superuser is not a control this change is adding to a panel it is otherwise leaving
    alone. Only `POST /auth/complete` builds this, and only when
    `services.registration_gate.admit_or_refuse` has said - under its advisory lock, in the
    transaction that does the insert - that the `user` table was empty.

    This is what makes the install docs' "the first account to sign in is yours" literally
    true, in both registration modes, without SQL or a script that is not in the shipped
    image.
    """

    is_superuser: bool = True


class UserUpdate(RejectsExplicitNulls):
    """`PATCH /user`'s body. Deliberately has no `email` field - changing an
    account's email requires proving ownership of the new address first (see
    `POST /user/email-change/request` / `POST /user/email-change/verify`),
    not a plain field update. `extra="forbid"` means submitting `email` here is a
    422, not a silently-ignored no-op, so callers notice they need the other flow.
    """

    model_config = ConfigDict(extra="forbid")

    # Every field here maps to a `NOT NULL` column. The avatar columns are deliberately
    # absent from this schema altogether - they are written by `PUT`/`DELETE /user/avatar`,
    # which own the blob beside them, and a PATCH that could null the key while leaving the
    # file on the volume is exactly the orphan this app has a sweeper for.
    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = (
        "name",
        "username",
        "gear_service_emails",
        "units",
    )

    name: Annotated[str | None, Field(min_length=2, max_length=30, examples=["User Userberg"], default=None)]
    username: Annotated[
        str | None, Field(min_length=2, max_length=20, pattern=r"^[a-z0-9]+$", examples=["userberg"], default=None)
    ]
    # Must be listed here as well as on `UserRead`: this schema is `extra="forbid"`, so
    # without it the settings page's toggle would 422 rather than save.
    gear_service_emails: Annotated[
        bool | None, Field(default=None, description="Email me when gear is due for service")
    ]
    # Same `extra="forbid"` reasoning as its neighbour: without this field the settings
    # page's units select would 422 rather than save.
    units: Annotated[
        UnitSystem | None, Field(default=None, description="Measurement system to display and accept values in")
    ]


class UserUpdateInternal(UserUpdate):
    updated_at: datetime


class UserAdminUpdate(UserUpdate):
    """Same as `UserUpdate`, but also allows setting `email` directly - reserved for
    the admin panel (`admin/views.py`'s `update_schema`), where a trusted superuser
    may need to fix up an account without going through the verified email-change
    flow. Never used by the public API.
    """

    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = (*UserUpdate.NON_NULLABLE_FIELDS, "email")

    email: Annotated[EmailStr | None, Field(examples=["user.userberg@example.com"], default=None)]


class AvatarRead(BaseModel):
    """`PUT /user/avatar`'s body: the stored image's hex digest, and nothing else.

    The same value `UserRead.avatar_sha256` carries, returned here so a client that has
    just uploaded can render the new picture without waiting on a `GET /user` round trip -
    it is the `ETag` on the download route and the `?v=` token that gives each version its
    own cache entry. Nothing about the *upload* is echoed back: byte size, content type and
    original filename all stop being true the moment the bytes are re-encoded.
    """

    sha256: Annotated[str, Field(examples=["e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"])]


class AccountDeletionResponse(BaseModel):
    """`DELETE /user`'s body. The `purge_after` date is the whole point of it.

    The account goes dark immediately, so there is no in-app countdown to read it off
    later - the client shows this date on the goodbye screen and the confirmation email
    repeats it. Returned before the email is attempted, deliberately: a relay failure
    then costs the copy, not the date.
    """

    message: str
    purge_after: datetime


class UserDelete(BaseModel):
    """FastCRUD's soft-delete payload - both columns, which is what makes `deleted_at` the
    deletion clock `purge_deleted_accounts` counts from.

    There is deliberately no `UserRestoreDeleted` beside it any more. It carried
    `is_deleted` alone, so restoring through it would have cleared the flag and left the
    clock set - `is_deleted = false, deleted_at = <a date>` looks alive but is a row nothing
    reconciles, and the mirror state (`true`, `NULL`) is the never-purge one the job warns
    about. `POST /auth/restore` clears both in one statement under a row lock; a schema that
    can only clear one is a trap wearing the right name.
    """

    model_config = ConfigDict(extra="forbid")

    is_deleted: bool
    deleted_at: datetime
