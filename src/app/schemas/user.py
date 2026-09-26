from collections.abc import Mapping
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Any, ClassVar

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field, StringConstraints, field_validator

from ..core.schemas import PublicUUIDSchema, RejectsExplicitNulls, StoredVocabulary
from .dive_form_preset import DiveFormField, canonical_hidden_fields
from .user_picture import PictureCrop


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


#: The account columns a dive shop's desk asks for, in the order a form asks for them.
#: Written once here because three places need the same list and must not spell it three
#: ways: the two schemas below declare each field, and the model carries each column.
#: Adding a column means adding it here, on the model, and on both schemas -
#: `tests/test_check_in_details.py` fails on any of the three being missed.
CHECK_IN_FIELDS: tuple[str, ...] = (
    "date_of_birth",
    "phone",
    "emergency_contact_name",
    "emergency_contact_phone",
    "emergency_contact_relationship",
    "insurance_provider",
    "insurance_policy_number",
    "insurance_expires_on",
)

#: The two check-in details that are one object each rather than one value, as their columns
#: with the anchor first. DiveJSON makes the anchor REQUIRED inside each object (§6.1), so a
#: contact nobody is named in, or a policy number with no insurer, is not a detail at all:
#: `PATCH /user` refuses to leave one behind, the export omits one already stored, and an
#: import drops one a document carries.
EMERGENCY_CONTACT_FIELDS: tuple[str, ...] = (
    "emergency_contact_name",
    "emergency_contact_phone",
    "emergency_contact_relationship",
)
INSURANCE_FIELDS: tuple[str, ...] = ("insurance_provider", "insurance_policy_number", "insurance_expires_on")

#: What the 422 says about a missing anchor, keyed by the anchor's column.
ANCHOR_REQUIRED_MESSAGES: dict[str, str] = {
    "emergency_contact_name": "Required while the emergency contact has a phone or a relationship",
    "insurance_provider": "Required while the insurance has a policy number or an expiry date",
}


def is_blank(value: object) -> bool:
    """Unset, for a check-in detail: `None`, or a string with nothing but whitespace in it.

    The columns take `""` from `PATCH /user` as readily as `null`, and a contact named `""`
    is no more a contact than one named nothing - so every rule about whether a detail is
    there asks this rather than `is None`.
    """
    return value is None or (isinstance(value, str) and not value.strip())


def lacks_its_anchor(values: Mapping[str, Any], fields: tuple[str, ...]) -> bool:
    """Whether `values` holds something of one object's but not the member that names it."""
    anchor, *members = fields
    return is_blank(values.get(anchor)) and any(not is_blank(values.get(member)) for member in members)


def _not_in_the_future(value: date) -> date:
    """A mistyped year is the whole of what this catches, and it reaches a dive shop as a
    diver who is not born yet.

    Only on the way in: `UserRead` carries the same field unguarded, so a row that somehow
    holds one still reads back rather than 500-ing the account on every authenticated
    request. There is no floor beneath it either - a plausible oldest diver is a guess, and
    refusing a real one is worse than storing an odd one.
    """
    if value > datetime.now(UTC).date():
        raise ValueError("a date of birth cannot be in the future")
    return value


# The check-in details' write-side types, stated once for the two routes that write them:
# `PATCH /user` from the settings form, and the logbook import from what the diver confirmed
# in its preview. Each bound is the column's width.
BirthDate = Annotated[date, AfterValidator(_not_in_the_future)]
CheckInPhone = Annotated[str, StringConstraints(max_length=32)]
CheckInName = Annotated[str, StringConstraints(max_length=255)]
CheckInShortText = Annotated[str, StringConstraints(max_length=64)]


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
    # The two pictures, read with the account in one query (`read_account`). Each
    # `*_sha256` is its rendition's digest: null means there is no picture - initials for
    # the avatar, an empty frame for the portrait - and non-null is the version token to
    # append to `GET /user/{avatar,portrait}` as `?v=`, so a replaced picture lands on a
    # fresh URL. There is no URL here on purpose: the bytes need a bearer token, so the
    # clients fetch them through their API client rather than putting a src on an `<img>`.
    #
    # `*_original_sha256` and `*_crop` are null together, when no original is held. The
    # digest is what offers "Adjust" and the `?v=` of `GET /user/{avatar,portrait}/original`;
    # the crop is where the adjustment opens.
    avatar_sha256: str | None = None
    avatar_original_sha256: str | None = None
    avatar_crop: PictureCrop | None = None
    portrait_sha256: str | None = None
    portrait_original_sha256: str | None = None
    portrait_crop: PictureCrop | None = None
    # Feeds the settings page's gear-reminder toggle. The `= True` is what `/openapi.json`
    # publishes as the field's default; it is *not* a fallback for a database missing the
    # column, which is what this said for a while. `get_current_user` selects every mapped
    # column, so such a database raises `UndefinedColumn` and the request 500s before
    # Pydantic sees a row at all (see DECISIONS.md).
    gear_service_emails: bool = True
    # The settings page's renewal-reminder and year-in-review toggles. Same note as the gear
    # toggle above on what the default is and isn't for.
    renewal_reminder_emails: bool = True
    year_in_review_emails: bool = True
    # Feeds the settings page's units toggle, and every measurement the web app renders.
    # Same note as its neighbour above on what the default is and isn't for.
    # `StoredVocabulary`, not `UnitSystem` - `user.units` is a plain `VARCHAR(16)` with
    # no DB `CHECK`, like every other vocabulary column, and `get_current_user` validates
    # the whole row through this schema on every authenticated request. See *"A stored
    # vocabulary is read back as a string"* in DECISIONS.md. `UserUpdate` keeps the enum.
    units: StoredVocabulary = UnitSystem.METRIC
    # Which dive-form fields this diver keeps hidden. Read on `GET /user` and carried by
    # `get_current_user`, so the form's first paint already omits them - which is the whole
    # reason this lives on the account rather than on the device. Same note as its two
    # neighbours above on what the default is and isn't for.
    dive_form_hidden_fields: Annotated[list[StoredVocabulary], Field(default_factory=list)]
    # The check-in details, null until the diver fills one in. Unconstrained here although
    # the columns are bounded and `UserUpdate` says so: this schema re-validates the stored
    # row on every authenticated request (`get_current_user`), so a rule stated here answers
    # a request the diver is making now for a value written long ago - the reasoning behind
    # `StoredVocabulary` beside it. The write side is where a bad value is refused.
    date_of_birth: date | None = None
    phone: str | None = None
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None
    emergency_contact_relationship: str | None = None
    insurance_provider: str | None = None
    insurance_policy_number: str | None = None
    insurance_expires_on: date | None = None
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

    # Every field here maps to a `NOT NULL` column. Neither picture is here at all - each is
    # written by its own routes under `/user/avatar` and `/user/portrait`, which own the blobs
    # beside the row, and a PATCH that could null a key while leaving the file in the store
    # is exactly the orphan this app has a sweeper for.
    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = (
        "name",
        "username",
        "gear_service_emails",
        "renewal_reminder_emails",
        "year_in_review_emails",
        "units",
        "dive_form_hidden_fields",
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
    # Same reasoning for the settings page's other two email toggles.
    renewal_reminder_emails: Annotated[
        bool | None,
        Field(default=None, description="Email me when a certification or my dive insurance is about to expire"),
    ]
    year_in_review_emails: Annotated[
        bool | None, Field(default=None, description="Email me a review of my diving year each January")
    ]
    # Same `extra="forbid"` reasoning as its neighbour: without this field the settings
    # page's units select would 422 rather than save.
    units: Annotated[
        UnitSystem | None, Field(default=None, description="Measurement system to display and accept values in")
    ]
    # Same `extra="forbid"` reasoning again: without this field the dive form's Fields panel
    # would 422 rather than save a toggle. Replaced wholesale - there is no "hide this one
    # more" verb, because the panel holds the whole set and sends it.
    dive_form_hidden_fields: Annotated[
        list[DiveFormField] | None,
        Field(
            default=None,
            max_length=len(DiveFormField),
            description="Dive form fields to keep hidden, in any order - stored in form order, duplicates collapsed",
        ),
    ]

    # The check-in details. Each is absent from `NON_NULLABLE_FIELDS` on purpose: the
    # columns are nullable, and an explicit `null` is how a diver removes a detail they
    # once gave - the emergency contact above all, which is three fields cleared together.
    # An emergency contact or an insurance left holding a member but not its anchor is
    # refused by the route, which is the one place that sees the row the patch lands on.
    date_of_birth: Annotated[
        BirthDate | None,
        Field(default=None, examples=["1988-04-12"], description="Date of birth, as a shop's form asks"),
    ]
    phone: Annotated[CheckInPhone | None, Field(default=None, examples=["+20 100 123 4567"])]
    emergency_contact_name: Annotated[CheckInName | None, Field(default=None)]
    emergency_contact_phone: Annotated[CheckInPhone | None, Field(default=None)]
    emergency_contact_relationship: Annotated[
        CheckInShortText | None, Field(default=None, examples=["Partner"], description="Free text, not a vocabulary")
    ]
    insurance_provider: Annotated[CheckInName | None, Field(default=None, examples=["DAN Europe"])]
    insurance_policy_number: Annotated[CheckInShortText | None, Field(default=None)]
    insurance_expires_on: Annotated[
        date | None, Field(default=None, description="Expiry of the dive insurance policy named above")
    ]

    @field_validator("dive_form_hidden_fields")
    @classmethod
    def _canonicalize_hidden_fields(cls, value: list[DiveFormField] | None) -> list[DiveFormField] | None:
        """The same canonical form a preset's `hidden_fields` is stored in, so "does the
        current state equal this preset?" stays a list comparison for the client.
        """
        return None if value is None else canonical_hidden_fields(value)


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
