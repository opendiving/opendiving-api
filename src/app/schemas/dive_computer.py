from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

# A recording's `device_serial` column holds at most this many characters, so no longer
# serial can name a computer any recording does.
SERIAL_MAX_LENGTH = 64
# A diver has a handful of computers; the list is bounded so the column stays small.
MAX_DIVE_COMPUTERS = 100


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
