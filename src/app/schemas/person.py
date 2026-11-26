import uuid as uuid_pkg
from datetime import datetime
from enum import StrEnum
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, model_validator

from ..core.schemas import MAX_LIST_ITEMS, NOTES_MAX_LENGTH, PublicUUIDSchema, RejectsExplicitNulls, StoredVocabulary
from .custom_property import CustomPropertiesRead, CustomPropertiesUpdate, CustomPropertiesWrite
from .link import LinksRead, LinksUpdate, LinksWrite

# DiveJSON §6.20's bounds, and the widths of the columns behind them.
PERSON_NAME_MAX = 255
PERSON_EMAIL_MAX = 255
PERSON_PHONE_MAX = 32
# A connect link's token: `secrets.token_hex(16)`.
CONNECT_LINK_TOKEN_MAX = 32

PERSON_NOT_FOUND = "Person not found."


class PersonRole(StrEnum):
    """What a person was on one occasion - DiveJSON §6.20's vocabulary, value for value and
    in its order.

    One value per reference rather than a set: where two apply the more specific wins, and
    the role belongs to the occasion rather than the person. Absent means *was there*, which
    is why there is no `other`.
    """

    BUDDY = "buddy"
    GUIDE = "guide"
    INSTRUCTOR = "instructor"
    STUDENT = "student"
    COMPANION = "companion"


# Stored trimmed, so the per-diver unique index on `lower(name)` is the trimmed key.
PersonName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=PERSON_NAME_MAX),
    Field(examples=["Alex M."], description="Name the diver knows the person by, unique among the diver's people"),
]
PersonEmail = Annotated[
    EmailStr | None, Field(default=None, max_length=PERSON_EMAIL_MAX, description="Email address of the person")
]
PersonPhone = Annotated[
    str | None,
    Field(default=None, max_length=PERSON_PHONE_MAX, description="Phone number of the person, free text"),
]
ConnectLinkToken = Annotated[
    str | None,
    Field(
        default=None,
        max_length=CONNECT_LINK_TOKEN_MAX,
        description="The token of a live connect link: the person is connected to the link's owner",
        examples=["9f86d081884c7d659a2feaa0c55ad015"],
    ),
]
ConnectAccount = Annotated[
    uuid_pkg.UUID | None,
    Field(
        default=None,
        description="The public id of an account that has a person connected to you, as `GET /people/added-you` "
        "lists it: the person is connected to that account",
    ),
]


class PersonReference(BaseModel):
    """One person on one dive, trip or course, on the way in."""

    model_config = ConfigDict(extra="forbid")

    person_uuid: Annotated[uuid_pkg.UUID, Field(description="Public id of one of the diver's people")]
    role: Annotated[PersonRole | None, Field(default=None, description="Absent means the person was there")]


class PersonReferenceRead(BaseModel):
    """One person on one dive, trip or course, as a read carries it: the uuid and the role,
    never a summary of the person. The role is a string - see *"A stored vocabulary is read
    back as a string"* in DECISIONS.md."""

    person_uuid: uuid_pkg.UUID
    role: StoredVocabulary | None = None


PeopleWrite = Annotated[
    list[PersonReference],
    Field(
        default_factory=list,
        max_length=MAX_LIST_ITEMS,
        description="The people on it, in the diver's order, each with the role they had; a person named "
        "twice keeps the first",
    ),
]
PeopleUpdate = Annotated[
    list[PersonReference] | None,
    Field(
        default=None,
        max_length=MAX_LIST_ITEMS,
        description="Replaces the people on it wholesale. Omit to leave them.",
    ),
]
PeopleRead = Annotated[
    list[PersonReferenceRead],
    Field(default_factory=list, description="The people on it, in the diver's order, each with the role they had"),
]


class PersonBase(CustomPropertiesWrite):
    name: PersonName
    email: PersonEmail
    phone: PersonPhone
    # The person's own pages - a profile, a social account - to open, never an identity: two
    # people with one address are two people, and nothing matches on it.
    links: LinksWrite
    notes: Annotated[
        str, Field(default="", max_length=NOTES_MAX_LENGTH, description="The diver's free-text notes on the person")
    ]


class PersonRead(CustomPropertiesRead, PublicUUIDSchema):
    """A person as `GET /people` and `GET /person/{uuid}` serve it.

    `username` and `avatar_sha256` are the connected account's **current** ones - joined on
    every read, so a rename or a new picture shows through and a purged account reads null -
    and nothing else of that account. `dive_count` is the live dives that name the person.
    """

    name: str
    email: str | None = None
    phone: str | None = None
    links: LinksRead
    notes: str = ""
    username: Annotated[
        str | None,
        Field(default=None, description="The connected account's current username, or null when not connected"),
    ]
    avatar_sha256: Annotated[
        str | None,
        Field(
            default=None,
            description="The connected account's picture version, passed as `v` to `GET /person/{uuid}/avatar`; "
            "null when the person is not connected, the account has no picture or has asked to be deleted",
        ),
    ]
    dive_count: Annotated[int, Field(description="Live dives whose `people` name this person")]
    created_at: datetime
    updated_at: datetime | None = None


class PersonReadInternal(CustomPropertiesRead, PublicUUIDSchema):
    """Mirrors the `person` table's columns, for server-side lookups only."""

    id: int
    user_id: int
    connected_user_id: int | None = None
    connection_hidden_at: datetime | None = None
    name: str
    email: str | None = None
    phone: str | None = None
    links: LinksRead
    notes: str = ""
    created_at: datetime
    updated_at: datetime | None = None


class ConnectsAPerson(BaseModel):
    """The two ways a person write connects one: at most one of them in a body."""

    connect_link: ConnectLinkToken
    connect_account: ConnectAccount

    @model_validator(mode="after")
    def one_way_at_most(self) -> ConnectsAPerson:
        given = [name for name in ("connect_link", "connect_account") if name in self.model_fields_set]
        if len(given) > 1:
            raise ValueError(f"Send one of {', '.join(given)}, not several.")
        return self


class PersonCreate(PersonBase, ConnectsAPerson):
    model_config = ConfigDict(extra="forbid")


class PersonCreateInternal(PersonBase):
    model_config = ConfigDict(extra="forbid")

    user_id: int
    connected_user_id: int | None = None


class PersonUpdate(CustomPropertiesUpdate, RejectsExplicitNulls):
    """The column shape of a person's PATCH, which `test_update_explicit_nulls.py` sweeps
    against the table. The tie is `PersonUpdateRequest`'s."""

    model_config = ConfigDict(extra="forbid")

    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ("name", "links", "notes", "custom_properties")

    name: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=PERSON_NAME_MAX),
        Field(None, description="Name the diver knows the person by, unique among the diver's people; cannot be null"),
    ]
    email: PersonEmail
    phone: PersonPhone
    links: LinksUpdate
    notes: Annotated[
        str | None,
        Field(
            default=None,
            max_length=NOTES_MAX_LENGTH,
            description="The diver's free-text notes on the person; cannot be null",
        ),
    ]


class PersonUpdateRequest(PersonUpdate, ConnectsAPerson):
    """Request body for `PATCH /person/{uuid}`. `connect_link` or `connect_account` connects a
    person who is not connected yet; omitting both leaves the tie alone."""


class PersonUpdateInternal(PersonUpdate):
    updated_at: datetime


class PersonLookupItem(PublicUUIDSchema):
    """One row of `GET /people/lookup`: the name and the connected account's current username."""

    name: str
    username: str | None = None


class AddedYouRow(PublicUUIDSchema):
    """One row of `GET /people/added-you`: an account that has the caller in its people, by its
    public id, its current name, username and picture version."""

    name: str
    username: str
    avatar_sha256: Annotated[
        str | None,
        Field(
            default=None,
            description="The account's picture version, passed as `v` to `GET /people/added-you/{uuid}/avatar`; "
            "null without one",
        ),
    ]
