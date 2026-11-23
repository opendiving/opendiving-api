import uuid as uuid_pkg
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from ..core.utils.datetime_offset import refuse_full_date

# A recording's `device_serial` column holds at most this many characters, so no longer
# serial can name a computer any recording does.
SERIAL_MAX_LENGTH = 64
# A diver has a handful of computers; the list is bounded so the column stays small.
MAX_DIVE_COMPUTERS = 100
# The dives one question may list. It bounds the uuids one statement binds; a client whose
# computer lists more asks in parts.
MAX_LISTED_DIVES = 2000


class DiveComputerRead(BaseModel):
    """One dive computer of the account, at its place in the account's order.

    The computers are the serials the account's recordings name, and those the diver has
    placed before any recording of them arrived. The three labels are the latest recording's
    of that serial, and all null for a serial no recording names yet.
    """

    serial: Annotated[
        str,
        Field(
            examples=["D9772626"],
            description="The computer's serial, as its latest recording spells it - or, where no recording names it, "
            "as it was placed, trimmed and case-folded. Two serials equal once trimmed and case-folded are one "
            "computer.",
        ),
    ]
    brand: Annotated[str | None, Field(default=None, examples=["Shearwater"], description="The maker")]
    model: Annotated[str | None, Field(default=None, examples=["Perdix 3"], description="The product string")]
    name: Annotated[
        str | None,
        Field(default=None, examples=["Porvoo"], description="What the computer calls itself, as its owner set it"),
    ]
    position: Annotated[int, Field(examples=[0], description="Its place in the order, 0 for the first")]


class DiveComputerOrder(BaseModel):
    """The whole order of an account's dive computers, first to last, by serial."""

    model_config = ConfigDict(extra="forbid")

    serials: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=SERIAL_MAX_LENGTH)]],
        Field(
            max_length=MAX_DIVE_COMPUTERS,
            examples=[["D9772626", "253810000400"]],
            description="Every serial in the order, first to last. A serial no recording names yet is kept. Serials "
            "are compared trimmed and case-folded, and two that are equal so are a 422.",
        ),
    ]


class ListedDive(BaseModel):
    """One dive as a computer lists it, before it is downloaded."""

    model_config = ConfigDict(extra="forbid")

    uuid: Annotated[
        uuid_pkg.UUID,
        Field(
            examples=["0193f5a2-7c1e-7b4a-9d2e-5f8a1c3b6d40"],
            description="The uuid the client would give this dive were it imported. A logbook dive of this uuid holds "
            "it.",
        ),
    ]
    started_at: Annotated[
        datetime,
        BeforeValidator(refuse_full_date),
        Field(
            examples=["2026-10-26T15:03:23"],
            description="The dive's start as the computer lists it: a date and time, with a UTC offset where the "
            "computer logs one and without where it logs a wall clock. A date alone is a 422. Compared as instants "
            "with a recording whose start carries an offset, and as wall clocks otherwise.",
        ),
    ]


class DiveComputerListing(BaseModel):
    """A computer's serial and the dives it lists, asked of the logbook before any is downloaded."""

    model_config = ConfigDict(extra="forbid")

    serial: Annotated[
        str,
        Field(
            min_length=1,
            max_length=SERIAL_MAX_LENGTH,
            examples=["D9772626"],
            description="The computer's serial. Compared with the serials the caller's recordings name, trimmed and "
            "case-folded.",
        ),
    ]
    dives: Annotated[
        list[ListedDive],
        Field(
            max_length=MAX_LISTED_DIVES,
            examples=[[{"uuid": "0193f5a2-7c1e-7b4a-9d2e-5f8a1c3b6d40", "started_at": "2026-10-26T15:03:23"}]],
            description="The dives the computer lists, at most 2,000; a computer listing more is asked in parts.",
        ),
    ]


class KnownDive(BaseModel):
    """A listed dive the caller's logbook holds, the dive that holds it, and whether that dive
    keeps the computer's original download of it - the one thing a download of it can still add."""

    uuid: Annotated[
        uuid_pkg.UUID,
        Field(examples=["0193f5a2-7c1e-7b4a-9d2e-5f8a1c3b6d40"], description="The listed dive's uuid, as sent"),
    ]
    dive_uuid: Annotated[
        uuid_pkg.UUID,
        Field(examples=["0193f0e1-2b3c-7d4e-8f50-6a7b8c9d0e1f"], description="The caller's dive that holds it"),
    ]
    has_original: Annotated[
        bool,
        Field(
            examples=[False],
            description="Whether the dive that holds it keeps this computer's original download of the listed dive. "
            "False wherever that cannot be told without reading a stored file.",
        ),
    ]
