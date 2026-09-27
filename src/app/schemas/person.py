import uuid as uuid_pkg
from datetime import datetime
from enum import StrEnum
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

from ..core.schemas import NOTES_MAX_LENGTH, PublicUUIDSchema, RejectsExplicitNulls, StoredVocabulary

# DiveJSON §6.20's bounds, and the widths of the columns behind them.
PERSON_NAME_MAX = 255
PERSON_EMAIL_MAX = 255
PERSON_PHONE_MAX = 32
# A username's own bounds (`schemas/user.py`): nothing longer names an account.
USERNAME_MAX = 20

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
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=PERSON_NAME_MAX), Field(examples=["Alex M."])
]
PersonEmail = Annotated[EmailStr | None, Field(default=None, max_length=PERSON_EMAIL_MAX)]
PersonPhone = Annotated[str | None, Field(default=None, max_length=PERSON_PHONE_MAX)]
LinkedUsername = Annotated[
    str | None,
    Field(
        default=None,
        max_length=USERNAME_MAX,
        description="The exact username of an account on this instance that this person is",
        examples=["alexm"],
    ),
]


class PersonReference(BaseModel):
    """One person on one dive, trip or course, on the way in."""

    model_config = ConfigDict(extra="forbid")

    person_uuid: uuid_pkg.UUID
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
        description="The people on it, in the diver's order, each with the role they had; a person named "
        "twice keeps the first",
    ),
]
PeopleUpdate = Annotated[
    list[PersonReference] | None,
    Field(default=None, description="Replaces the people on it wholesale. Omit to leave them."),
]
PeopleRead = Annotated[
    list[PersonReferenceRead],
    Field(default_factory=list, description="The people on it, in the diver's order, each with the role they had"),
]


class PersonBase(BaseModel):
    name: PersonName
    email: PersonEmail
    phone: PersonPhone
    notes: Annotated[str, Field(default="", max_length=NOTES_MAX_LENGTH)]


class PersonRead(PublicUUIDSchema):
    """A person as `GET /people` and `GET /person/{uuid}` serve it.

    `username` is the linked account's **current** username - joined on every read, so a
    rename shows through and a purged account reads null - and nothing else of that account.
    `dive_count` is the live dives that name the person.
    """

    name: str
    email: str | None = None
    phone: str | None = None
    notes: str = ""
    username: Annotated[
        str | None, Field(default=None, description="The linked account's current username, or null when unlinked")
    ]
    dive_count: Annotated[int, Field(description="Live dives whose `people` name this person")]
    created_at: datetime
    updated_at: datetime | None = None


class PersonReadInternal(PublicUUIDSchema):
    """Mirrors the `person` table's columns, for server-side lookups only."""

    id: int
    user_id: int
    linked_user_id: int | None = None
    name: str
    email: str | None = None
    phone: str | None = None
    notes: str = ""
    created_at: datetime
    updated_at: datetime | None = None


class PersonCreate(PersonBase):
    model_config = ConfigDict(extra="forbid")

    username: LinkedUsername


class PersonCreateInternal(PersonBase):
    model_config = ConfigDict(extra="forbid")

    user_id: int
    linked_user_id: int | None = None


class PersonUpdate(RejectsExplicitNulls):
    """The column shape of a person's PATCH, which `test_update_explicit_nulls.py` sweeps
    against the table. The link is `PersonUpdateRequest`'s."""

    model_config = ConfigDict(extra="forbid")

    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ("name", "notes")

    name: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=PERSON_NAME_MAX), Field(None)
    ]
    email: PersonEmail
    phone: PersonPhone
    notes: Annotated[str | None, Field(default=None, max_length=NOTES_MAX_LENGTH)]


class PersonUpdateRequest(PersonUpdate):
    """Request body for `PATCH /person/{uuid}`. `username` links the person to that account;
    `null` detaches, and omitting it leaves the link alone."""

    username: LinkedUsername


class PersonUpdateInternal(PersonUpdate):
    updated_at: datetime
