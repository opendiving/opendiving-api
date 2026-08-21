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
    profile_image_url: str
    # Feeds the settings page's gear-reminder toggle. The `= True` is what `/openapi.json`
    # publishes as the field's default; it is *not* a fallback for a database missing the
    # column, which is what this said for a while. `get_current_user` selects every mapped
    # column, so such a database raises `UndefinedColumn` and the request 500s before
    # Pydantic sees a row at all (see DECISIONS.md).
    gear_service_emails: bool = True
    # Feeds the settings page's units toggle, and every measurement the web app renders.
    # Same note as its neighbour above on what the default is and isn't for.
    units: UnitSystem = UnitSystem.METRIC


class UserReadInternal(UserRead):
    """Adds the internal sequential `id`, for server-side lookups only - never returned
    directly over the API (use `UserRead` for that).
    """

    id: int


class UserCreateInternal(UserBase):
    """The only way a `User` row is ever created - from a completed profile (`POST
    /auth/complete`), never directly from a signup form. There's no password field
    anywhere: identity is proven up front by the email-magic-link or Google flow, and
    the resulting authentication method is recorded separately (see
    `AuthenticationProviderCreate`), not on the user row itself.
    """

    profile_image_url: str = "https://profileimageurl.com"


class UserUpdate(RejectsExplicitNulls):
    """`PATCH /user`'s body. Deliberately has no `email` field - changing an
    account's email requires proving ownership of the new address first (see
    `POST /user/email-change/request` / `POST /user/email-change/verify`),
    not a plain field update. `extra="forbid"` means submitting `email` here is a
    422, not a silently-ignored no-op, so callers notice they need the other flow.
    """

    model_config = ConfigDict(extra="forbid")

    # Every field here maps to a `NOT NULL` column - `profile_image_url` included, which
    # carries a placeholder URL rather than a null when a user has no picture.
    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = (
        "name",
        "username",
        "profile_image_url",
        "gear_service_emails",
        "units",
    )

    name: Annotated[str | None, Field(min_length=2, max_length=30, examples=["User Userberg"], default=None)]
    username: Annotated[
        str | None, Field(min_length=2, max_length=20, pattern=r"^[a-z0-9]+$", examples=["userberg"], default=None)
    ]
    profile_image_url: Annotated[
        str | None,
        Field(
            pattern=r"^(https?|ftp)://[^\s/$.?#].[^\s]*$", examples=["https://www.profileimageurl.com"], default=None
        ),
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
    model_config = ConfigDict(extra="forbid")

    is_deleted: bool
    deleted_at: datetime


class UserRestoreDeleted(BaseModel):
    is_deleted: bool
