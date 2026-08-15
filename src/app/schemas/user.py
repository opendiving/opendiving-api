from datetime import datetime
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from ..core.schemas import PublicUUIDSchema, RejectsExplicitNulls


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
    # Feeds the settings page's gear-reminder toggle. Defaults to `True` so this still
    # validates against a database where the column hasn't been added by hand yet (see
    # DECISIONS.md's "no migration tool" workflow).
    gear_service_emails: bool = True


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
    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ("name", "username", "profile_image_url", "gear_service_emails")

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


class UserDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_deleted: bool
    deleted_at: datetime


class UserRestoreDeleted(BaseModel):
    is_deleted: bool
