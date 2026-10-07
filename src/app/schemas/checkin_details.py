"""The check-in details: what a dive shop's desk asks a diver for, held once.

One object per account, read and written at `/user/checkin-details`: the email the diver
gives out, a phone, a date of birth, and two ordered lists - emergency contacts in call order
and insurance policies in the diver's. The bounds are DiveJSON §6.1's for the member each
travels as, and live on the write side only; the read side is unconstrained, per *A stored
vocabulary is read back as a string* in DECISIONS.md.
"""

from datetime import UTC, date, datetime
from typing import Annotated, ClassVar

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, EmailStr, Field, StringConstraints

from ..core.schemas import RejectsExplicitNulls

EMAIL_MAX = 255
PHONE_MAX = 32
NAME_MAX = 255
SHORT_TEXT_MAX = 64

# Bounds the request body and the sheet; the format states none.
MAX_EMERGENCY_CONTACTS = 5
MAX_INSURANCE_POLICIES = 5


def is_blank(value: object) -> bool:
    """Unset, for a check-in detail a document carries: `None`, or a string with nothing but
    whitespace in it - the format's `minLength` of 1 admits a space."""
    return value is None or (isinstance(value, str) and not value.strip())


def _not_in_the_future(value: date) -> date:
    """A mistyped year is the whole of what this catches, and it reaches a dive shop as a
    diver who is not born yet. No floor: refusing a real oldest diver is worse than storing
    an odd one."""
    if value > datetime.now(UTC).date():
        raise ValueError("a date of birth cannot be in the future")
    return value


def _stripped_or_none(value: object) -> object:
    if isinstance(value, str):
        return value.strip() or None
    return value


BirthDate = Annotated[date, AfterValidator(_not_in_the_future)]
# A row's anchor, refused when blank: a contact nobody is named in, or a policy with no
# insurer, is not a detail at all (DiveJSON §6.1 makes each REQUIRED).
_Anchor = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX)]
# An optional text member, stored null when nothing but whitespace was sent, so a reader never
# sees `""` of the object.
_Phone = Annotated[
    Annotated[str, StringConstraints(max_length=PHONE_MAX)] | None,
    BeforeValidator(_stripped_or_none),
    Field(default=None),
]
_ShortText = Annotated[
    Annotated[str, StringConstraints(max_length=SHORT_TEXT_MAX)] | None,
    BeforeValidator(_stripped_or_none),
    Field(default=None),
]
_Email = Annotated[
    Annotated[EmailStr, Field(max_length=EMAIL_MAX)] | None,
    BeforeValidator(_stripped_or_none),
    Field(default=None),
]


class EmergencyContact(BaseModel):
    """One emergency contact as read."""

    name: str
    phone: str | None = None
    relationship: str | None = None


class InsurancePolicy(BaseModel):
    """One insurance policy as read. The reminder bookkeeping on its row never crosses the
    wire."""

    provider: str
    number: str | None = None
    expires_on: date | None = None


class CheckinDetailsRead(BaseModel):
    """The check-in details. An account that has saved nothing reads as nulls and empty
    lists, never as a 404, so every reader renders one shape. `email` is the address the
    diver gives out; the sign-in address is `GET /user`'s and appears nowhere here."""

    email: str | None = None
    phone: str | None = None
    date_of_birth: date | None = None
    emergency_contacts: Annotated[
        list[EmergencyContact], Field(default_factory=list, description="In call order: the first is called first")
    ]
    insurance_policies: Annotated[
        list[InsurancePolicy], Field(default_factory=list, description="In the diver's order")
    ]


class EmergencyContactInput(BaseModel):
    """One emergency contact on the way in."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[_Anchor, Field(examples=["Grace Hopper"])]
    phone: _Phone
    relationship: Annotated[_ShortText, Field(examples=["Partner"], description="Free text, not a vocabulary")]


class InsurancePolicyInput(BaseModel):
    """One insurance policy on the way in."""

    model_config = ConfigDict(extra="forbid")

    provider: Annotated[_Anchor, Field(examples=["DAN Europe"])]
    number: Annotated[_ShortText, Field(examples=["DE-4471902"])]
    expires_on: Annotated[date | None, Field(default=None, examples=["2027-06-30"])]


class CheckinDetailsUpdate(RejectsExplicitNulls):
    """`PATCH /user/checkin-details`'s body. A key present replaces its member whole - a
    scalar with its value, `null` clearing it; a list as a unit, `[]` clearing it - and a key
    absent leaves its member as it was. Each surface sends the group it edits and nothing
    else, so a stale copy elsewhere can revert no more than that group.

    The lists are refused as `null`: `[]` is how one is cleared, and a `null` there is more
    likely a client that meant to leave it alone.
    """

    model_config = ConfigDict(extra="forbid")

    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ("emergency_contacts", "insurance_policies")

    email: Annotated[
        _Email,
        Field(
            examples=["desk@example.org"], description="The address the diver gives out, printed on the check-in sheet"
        ),
    ]
    phone: Annotated[_Phone, Field(examples=["+20 100 123 4567"])]
    date_of_birth: Annotated[BirthDate | None, Field(default=None, examples=["1988-04-12"])]
    emergency_contacts: Annotated[
        list[EmergencyContactInput] | None,
        Field(default=None, max_length=MAX_EMERGENCY_CONTACTS, description="In call order: the first is called first"),
    ]
    insurance_policies: Annotated[
        list[InsurancePolicyInput] | None,
        Field(default=None, max_length=MAX_INSURANCE_POLICIES, description="In the diver's order"),
    ]
