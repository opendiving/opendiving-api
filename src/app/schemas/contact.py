import uuid as uuid_pkg
from collections.abc import Iterable, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, ClassVar
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field, field_validator

from ..core.schemas import NOTES_MAX_LENGTH, PublicUUIDSchema, RejectsExplicitNulls, StoredVocabulary

# DiveJSON §6.18's bounds, and the widths of the columns behind them.
CONTACT_NAME_MAX = 255
CONTACT_PHONE_MAX = 32
CONTACT_EMAIL_MAX = 255
CONTACT_WEBSITE_MAX = 512
ADDRESS_LINE_MAX = 255
ADDRESS_POSTCODE_MAX = 32

WEBSITE_MESSAGE = "website must be an absolute http or https URL, e.g. https://example.com"


class ContactRole(StrEnum):
    """What a contact is to the diver, and the whole vocabulary of it - DiveJSON §6.18's
    `roles`, value for value and in its order.

    A **set**, not a type: a resort is `dive_center` + `accommodation`, a school that sells
    gear is `school` + `shop`, and there is no `resort` member for exactly that reason - an
    overlapping value would file one party two ways. `other` stops a forced lie (an
    aquarium, a navy school).

    Declaration order is the order every stored list is rewritten into
    (`canonical_roles`), so two equal sets are two equal lists. The column holds plain
    strings with no DB `CHECK`, like every other stored vocabulary here.
    """

    DIVE_CENTER = "dive_center"
    SCHOOL = "school"
    SHOP = "shop"
    ACCOMMODATION = "accommodation"
    LIVEABOARD = "liveaboard"
    CLUB = "club"
    OTHER = "other"


def canonical_roles(values: Iterable[ContactRole]) -> list[ContactRole]:
    """Collapse duplicates and impose `ContactRole`'s declaration order.

    Duplicates are folded rather than refused, as `canonical_hidden_fields` folds a
    preset's: the set is what the diver meant, and the order they ticked the boxes in says
    nothing.
    """
    present = set(values)
    return [role for role in ContactRole if role in present]


def check_website(value: str | None) -> str | None:
    """An absolute `http`/`https` URL with a host, or a `ValueError`.

    DiveJSON types the member `format: uri`, and no checker in either repository enforces
    that, so this is the one place it holds: the column stores a URL a link can open. A
    bare host is refused rather than given a scheme - the client prepends `https://`, and a
    guess made here would be a second copy of that rule.
    """
    if value is None:
        return None
    parts = urlsplit(value)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise ValueError(WEBSITE_MESSAGE)
    return value


ContactWebsite = Annotated[
    str | None,
    Field(default=None, max_length=CONTACT_WEBSITE_MAX, examples=["https://blueocean.example"]),
    AfterValidator(check_website),
]
ContactEmail = Annotated[
    EmailStr | None, Field(default=None, max_length=CONTACT_EMAIL_MAX, examples=["info@blueocean.example"])
]
ContactPhone = Annotated[str | None, Field(default=None, max_length=CONTACT_PHONE_MAX, examples=["+20 69 364 0000"])]


CONTACT_UUID_DESCRIPTION = "Public id of the contact - the dive center, school or club - that ran it"

# -------------- address --------------
#: The members an address carries, in the order the columns behind it are declared.
ADDRESS_FIELDS = ("street", "city", "postcode", "region", "country")

#: What `contact`'s address columns are called: flat and prefixed, as a dive site's
#: locality sits under `location_`.
CONTACT_ADDRESS_PREFIX = "address_"


class ContactAddressInput(BaseModel):
    """A postal address on the way in: DiveJSON §6.19, UDDF's `<address>` with `<province>`
    renamed `region`.

    `country` is the one required member, as it is in both formats: an address with no
    country is no anchor, and neither export could write it.
    """

    model_config = ConfigDict(extra="forbid")

    street: Annotated[str | None, Field(default=None, max_length=ADDRESS_LINE_MAX)]
    city: Annotated[str | None, Field(default=None, max_length=ADDRESS_LINE_MAX, examples=["Dahab"])]
    postcode: Annotated[str | None, Field(default=None, max_length=ADDRESS_POSTCODE_MAX)]
    region: Annotated[str | None, Field(default=None, max_length=ADDRESS_LINE_MAX, examples=["South Sinai"])]
    country: Annotated[str, Field(min_length=1, max_length=ADDRESS_LINE_MAX, examples=["Egypt"])]


class ContactAddressRead(BaseModel):
    """Public shape of an address. No id: it is replaced whole with the contact's write."""

    street: str | None = None
    city: str | None = None
    postcode: str | None = None
    region: str | None = None
    country: str


def address_columns(address: ContactAddressInput | None) -> dict[str, Any]:
    """An address spread across the contact's columns, every one of them named - `None`
    where there is no address, so a replacement clears what the old one held."""
    return {
        f"{CONTACT_ADDRESS_PREFIX}{field}": None if address is None else getattr(address, field)
        for field in ADDRESS_FIELDS
    }


def address_from_row(row: Any) -> ContactAddressRead | None:
    """An address read back off a contact row, or `None` where it holds none.

    `address_country` says whether there is one: `ck_contact_address_has_country` keeps
    every other column empty without it. Takes a mapping or an object, as
    `location_from_row` does.
    """
    read = (lambda field: row[field]) if isinstance(row, Mapping) else (lambda field: getattr(row, field))
    if read(f"{CONTACT_ADDRESS_PREFIX}country") is None:
        return None
    return ContactAddressRead(**{field: read(f"{CONTACT_ADDRESS_PREFIX}{field}") for field in ADDRESS_FIELDS})


class ContactAddressColumns(BaseModel):
    """The address as `contact` stores it: flat and prefixed. Never on the wire - the
    admin form and the internal read shapes use it."""

    address_street: Annotated[str | None, Field(default=None, max_length=ADDRESS_LINE_MAX)]
    address_city: Annotated[str | None, Field(default=None, max_length=ADDRESS_LINE_MAX)]
    address_postcode: Annotated[str | None, Field(default=None, max_length=ADDRESS_POSTCODE_MAX)]
    address_region: Annotated[str | None, Field(default=None, max_length=ADDRESS_LINE_MAX)]
    address_country: Annotated[str | None, Field(default=None, max_length=ADDRESS_LINE_MAX)]


# -------------- contact --------------
class ContactBase(BaseModel):
    """What every shape of a contact carries apart from the members whose write and read
    types differ - `roles`, `email` and `website` - and the address, which the wire nests
    and the table does not."""

    name: Annotated[str, Field(min_length=1, max_length=CONTACT_NAME_MAX, examples=["Blue Ocean Dive Center"])]
    phone: ContactPhone
    notes: Annotated[str, Field(default="", max_length=NOTES_MAX_LENGTH)]


class _ContactWriteFields(BaseModel):
    """The members a write validates and a read does not: the role vocabulary, the email
    address and the website's scheme. A read carries them as the strings they are - see
    *"A stored vocabulary is read back as a string"* in DECISIONS.md."""

    roles: Annotated[
        list[ContactRole],
        Field(
            default_factory=list,
            max_length=len(ContactRole),
            description="What this contact is to the diver, in any order - stored in vocabulary order, deduplicated",
            examples=[[ContactRole.DIVE_CENTER, ContactRole.ACCOMMODATION]],
        ),
    ]
    email: ContactEmail
    website: ContactWebsite

    @field_validator("roles")
    @classmethod
    def _canonicalize(cls, value: list[ContactRole]) -> list[ContactRole]:
        return canonical_roles(value)


class _ContactReadFields(BaseModel):
    roles: Annotated[list[StoredVocabulary], Field(default_factory=list)]
    email: str | None = None
    website: str | None = None


class ContactRead(ContactBase, _ContactReadFields, PublicUUIDSchema):
    """Public representation of a contact, keyed by its opaque `uuid`."""

    address: ContactAddressRead | None = None
    user_uuid: uuid_pkg.UUID
    created_at: datetime


class ContactReadInternal(ContactBase, _ContactReadFields, ContactAddressColumns, PublicUUIDSchema):
    """Mirrors the `contact` table's columns (integer PK/FK, flat address), for server-side
    lookups only - `ContactRead` is the public shape."""

    id: int
    user_id: int
    created_at: datetime


class ContactCreate(ContactBase, _ContactWriteFields):
    model_config = ConfigDict(extra="forbid")

    address: Annotated[
        ContactAddressInput | None, Field(default=None, description="Postal address; `country` required")
    ]


class ContactCreateInternal(ContactBase, _ContactWriteFields, ContactAddressColumns):
    """What reaches FastCRUD, so the address is flat here where `ContactCreate` nests it -
    and CRUDAdmin's create form, which is why it carries columns and nothing else."""

    model_config = ConfigDict(extra="forbid")

    user_id: int


class _ContactUpdateFields(RejectsExplicitNulls):
    """What both update shapes carry, which is everything but the address.

    `phone`, `email` and `website` are nullable and stay off `NON_NULLABLE_FIELDS`, so an
    explicit null clears them. `roles` is not nullable: a contact with no role is the empty
    list.
    """

    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ("name", "roles", "notes")

    name: Annotated[str | None, Field(default=None, min_length=1, max_length=CONTACT_NAME_MAX)]
    roles: Annotated[
        list[ContactRole] | None,
        Field(default=None, max_length=len(ContactRole), description="Replaces the contact's roles wholesale"),
    ]
    phone: ContactPhone
    email: ContactEmail
    website: ContactWebsite
    notes: Annotated[str | None, Field(default=None, max_length=NOTES_MAX_LENGTH)]

    @field_validator("roles")
    @classmethod
    def _canonicalize(cls, value: list[ContactRole] | None) -> list[ContactRole] | None:
        return None if value is None else canonical_roles(value)


class ContactUpdate(_ContactUpdateFields, ContactAddressColumns):
    """CRUDAdmin's Contact form, and the shape `test_update_explicit_nulls.py` sweeps
    against the `contact` table's columns - so the address is flat here, as the table has
    it. `DiveSiteUpdate` sits beside `DiveSiteUpdateRequest` for the same reason."""

    model_config = ConfigDict(extra="forbid")


class ContactUpdateRequest(_ContactUpdateFields):
    """Request body for `PATCH /contact/{uuid}`.

    Naming `address` **replaces** the stored one whole, as a dive site's `location` is
    replaced: a partial one would leave the old street under a new country. An explicit
    null clears it.
    """

    model_config = ConfigDict(extra="forbid")

    address: ContactAddressInput | None = None


class ContactUpdateInternal(ContactUpdate):
    updated_at: datetime
