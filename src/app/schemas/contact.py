import uuid as uuid_pkg
from collections.abc import Iterable, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from ..core.schemas import (
    NOTES_MAX_LENGTH,
    PublicUUIDSchema,
    RejectsExplicitNulls,
    StoredName,
    StoredVocabulary,
    Trimmed,
)
from .link import LINK_URL_MAX, Link, LinkRead, LinksRead, LinksUpdate, LinksWrite, canonical_links, check_link_url
from .location import Latitude, Longitude, WholeCoordinatePair

# DiveJSON §6.18's bounds, and the widths of the columns behind them.
CONTACT_NAME_MAX = 255
CONTACT_PHONE_MAX = 32
CONTACT_EMAIL_MAX = 255
ADDRESS_LINE_MAX = 255
ADDRESS_POSTCODE_MAX = 32


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


def _website(value: str | None) -> str | None:
    return None if value is None else check_link_url(value)


# The contact's first link's address under the member's old name, for a client that writes no
# `links` yet; it leaves the wire once none does. See *"A contact's `website` is its first
# link"* in DECISIONS.md.
ContactWebsite = Annotated[
    str | None,
    Field(
        default=None,
        max_length=LINK_URL_MAX,
        examples=["https://blueocean.example"],
        description="The first link's address, for a client that does not send `links`; not with `links`",
    ),
    AfterValidator(_website),
]
WEBSITE_WITH_LINKS = "Send links or website, not both"


def links_after_website(stored: list[LinkRead], website: str | None) -> list[dict[str, Any]] | None:
    """A contact's links once a write naming `website` alone has landed, or `None` where it
    changes nothing.

    The address equal to the stored first one is the echo of a client that sends the whole
    record on every save, and leaves the list as it is. Another one replaces the first entry's
    address and clears its label, the label having named the old page, or becomes the only
    entry; null removes the first entry.
    """
    first = stored[0].url if stored else None
    if website == first:
        return None
    rest = [Link.model_validate(entry.model_dump()) for entry in stored[1:]]
    if website is None:
        entries = rest
    else:
        entries = [Link(url=website, label=None), *rest]
    return [entry.model_dump() for entry in canonical_links(entries)]


ContactEmail = Annotated[
    EmailStr | None,
    Field(
        default=None,
        max_length=CONTACT_EMAIL_MAX,
        examples=["info@blueocean.example"],
        description="Email address of the contact",
    ),
]
ContactPhone = Annotated[
    str | None,
    Field(
        default=None,
        max_length=CONTACT_PHONE_MAX,
        examples=["+20 69 364 0000"],
        description="Phone number of the contact, free text",
    ),
]


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

    street: Annotated[
        str | None, Field(default=None, max_length=ADDRESS_LINE_MAX, description="Street and house number")
    ]
    city: Annotated[
        str | None,
        Field(default=None, max_length=ADDRESS_LINE_MAX, examples=["Dahab"], description="City, town or village"),
    ]
    postcode: Annotated[
        str | None, Field(default=None, max_length=ADDRESS_POSTCODE_MAX, description="Postal or ZIP code")
    ]
    region: Annotated[
        str | None,
        Field(
            default=None,
            max_length=ADDRESS_LINE_MAX,
            examples=["South Sinai"],
            description="State, province or other administrative region",
        ),
    ]
    country: Annotated[
        str, Field(min_length=1, max_length=ADDRESS_LINE_MAX, examples=["Egypt"], description="Country name")
    ]


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
    types differ - `roles`, `email` and `links` - and the address, which the wire nests
    and the table does not.

    The position is flat on the wire as in the table, as a dive site's pin is. Only the write
    shapes add `WholeCoordinatePair`, for the reason it gives for staying off a read."""

    name: Annotated[
        str,
        Trimmed,
        Field(
            max_length=CONTACT_NAME_MAX,
            examples=["Blue Ocean Dive Center"],
            description="Name of the contact, unique among the diver's contacts ignoring case",
        ),
    ]
    phone: ContactPhone
    latitude: Annotated[Latitude, Field(description="Latitude of the contact's position in decimal degrees")]
    longitude: Annotated[Longitude, Field(description="Longitude of the contact's position in decimal degrees")]
    notes: Annotated[
        str, Field(default="", max_length=NOTES_MAX_LENGTH, description="The diver's free-text notes on the contact")
    ]


class _ContactWriteFields(BaseModel):
    """The members a write validates and a read does not: the role vocabulary, the email
    address and the links. A read carries them as the strings they are - see
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
    links: LinksWrite

    @field_validator("roles")
    @classmethod
    def _canonicalize(cls, value: list[ContactRole]) -> list[ContactRole]:
        return canonical_roles(value)


class _ContactReadFields(BaseModel):
    roles: Annotated[list[StoredVocabulary], Field(default_factory=list)]
    email: str | None = None
    links: LinksRead


class ContactRead(ContactBase, _ContactReadFields, PublicUUIDSchema):
    """Public representation of a contact, keyed by its opaque `uuid`."""

    name: StoredName
    website: Annotated[
        str | None,
        Field(default=None, description="The first link's address, or null with no links; read `links` instead"),
    ]
    address: ContactAddressRead | None = None
    user_uuid: uuid_pkg.UUID
    created_at: datetime

    @model_validator(mode="after")
    def _website_is_the_first_link(self) -> Self:
        self.website = self.links[0].url if self.links else None
        return self


class ContactReadInternal(ContactBase, _ContactReadFields, ContactAddressColumns, PublicUUIDSchema):
    """Mirrors the `contact` table's columns (integer PK/FK, flat address), for server-side
    lookups only - `ContactRead` is the public shape."""

    name: StoredName
    id: int
    user_id: int
    created_at: datetime


class ContactCreate(ContactBase, _ContactWriteFields, WholeCoordinatePair):
    """`website` alone stores a single link with no label, and null or nothing stores none."""

    model_config = ConfigDict(extra="forbid")

    website: ContactWebsite
    address: Annotated[
        ContactAddressInput | None, Field(default=None, description="Postal address; `country` required")
    ]

    @model_validator(mode="after")
    def _website_is_the_first_link(self) -> Self:
        if "website" in self.model_fields_set:
            if "links" in self.model_fields_set:
                raise ValueError(WEBSITE_WITH_LINKS)
            self.links = canonical_links([] if self.website is None else [Link(url=self.website, label=None)])
        return self


class ContactCreateInternal(ContactBase, _ContactWriteFields, ContactAddressColumns, WholeCoordinatePair):
    """What reaches FastCRUD, so the address is flat here where `ContactCreate` nests it -
    and CRUDAdmin's create form, which is why it carries columns and nothing else."""

    model_config = ConfigDict(extra="forbid")

    user_id: int


class _ContactUpdateFields(WholeCoordinatePair, RejectsExplicitNulls):
    """What both update shapes carry, which is everything but the address.

    `phone` and `email` are nullable and stay off `NON_NULLABLE_FIELDS`, so an explicit null
    clears them. `roles` and `links` are not nullable: a contact with no role or no link is
    the empty list. `latitude` and `longitude` are nullable too, but answer to `WholeCoordinatePair`:
    they are named together or not at all, and both `null` clears the position.
    """

    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ("name", "roles", "links", "notes")

    name: Annotated[
        Annotated[str, Trimmed] | None,
        Field(
            default=None,
            max_length=CONTACT_NAME_MAX,
            description="Name of the contact, unique among the diver's contacts ignoring case; cannot be null",
        ),
    ]
    roles: Annotated[
        list[ContactRole] | None,
        Field(default=None, max_length=len(ContactRole), description="Replaces the contact's roles wholesale"),
    ]
    phone: ContactPhone
    email: ContactEmail
    links: LinksUpdate
    notes: Annotated[
        str | None,
        Field(
            default=None,
            max_length=NOTES_MAX_LENGTH,
            description="The diver's free-text notes on the contact; cannot be null",
        ),
    ]

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
    null clears it. `website`, sent without `links`, acts on the first stored link.
    """

    model_config = ConfigDict(extra="forbid")

    website: ContactWebsite
    address: Annotated[
        ContactAddressInput | None,
        Field(
            default=None,
            description="Replaces the postal address whole; `country` required. Null clears it, omitting leaves it",
        ),
    ]

    @model_validator(mode="after")
    def _links_or_website(self) -> Self:
        if {"website", "links"} <= self.model_fields_set:
            raise ValueError(WEBSITE_WITH_LINKS)
        return self


class ContactUpdateInternal(ContactUpdate):
    updated_at: datetime


class ContactLookupItem(PublicUUIDSchema):
    """One row of `GET /contacts/lookup`: the name, and the address a picker reads the town
    and country from."""

    name: str
    address: ContactAddressRead | None = None
