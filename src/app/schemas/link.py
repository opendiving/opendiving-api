"""A record's links: addresses for a person to open, each with an optional label - DiveJSON
§6.18's Link, without the `extensions` this app stores nowhere.

A contact, a person, a course, a gear item and a trip each keep an ordered list of them in a
JSON column of their own table, stored canonical: both members on every entry, a blank label
as null, an address named twice kept where it first appears. Nothing queries into the list.
"""

from collections.abc import Iterable
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from ..core.schemas import BlankIsNone

# DiveJSON §6.18's bounds on an entry. The format bounds no list; this app keeps what a
# two-box editor can show.
LINK_URL_MAX = 512
LINK_LABEL_MAX = 120
LINKS_MAX = 20

# The whitespace the DiveJSON schema refuses anywhere in an address: every character either
# ECMA-262's or Python's `\s` matches, spelled as code points because the two disagree.
# `urlsplit` drops some of them and passes the rest, so the check reads the string itself.
LINK_WHITESPACE = frozenset(
    chr(code)
    for code in (
        *range(0x0009, 0x000E),
        *range(0x001C, 0x0021),
        0x0085,
        0x00A0,
        0x1680,
        *range(0x2000, 0x200B),
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
        0xFEFF,
    )
)

LINK_URL_MESSAGE = "must be an absolute http or https URL, e.g. https://example.com"


def check_link_url(value: str) -> str:
    """An absolute `http`/`https` address with a host, opening on its scheme and holding no
    whitespace, or a `ValueError`.

    The format's own pattern admits any scheme; this app opens only these two. A bare host is
    refused rather than given a scheme - the clients prepend `https://`, and a guess made here
    would be a second copy of that rule. The scheme has to be the first thing in the string
    because `urlsplit` strips leading control characters and spaces before reading one.
    """
    space = next((character for character in value if character in LINK_WHITESPACE), None)
    if space is not None:
        raise ValueError(f"must hold no space or line break, and this one holds U+{ord(space):04X}")
    try:
        parts = urlsplit(value)
    except ValueError:
        raise ValueError(LINK_URL_MESSAGE) from None
    if parts.scheme not in ("http", "https") or not parts.netloc or not value.lower().startswith(f"{parts.scheme}:"):
        raise ValueError(LINK_URL_MESSAGE)
    return value


class Link(BaseModel):
    """One link on the way in."""

    model_config = ConfigDict(extra="forbid")

    url: Annotated[
        str,
        Field(
            max_length=LINK_URL_MAX,
            examples=["https://blueocean.example/booking"],
            description="Absolute http or https address to open, with no whitespace",
        ),
        AfterValidator(check_link_url),
    ]
    label: Annotated[
        str | None,
        Field(
            default=None,
            max_length=LINK_LABEL_MAX,
            examples=["Booking"],
            description="What the link is, shown in place of the address; blank or absent is none",
        ),
        BlankIsNone,
    ]


class LinkRead(BaseModel):
    """A link as stored: read back without the write's checks, a stored value being what it
    is."""

    url: str
    label: str | None = None


def canonical_links(entries: Iterable[Link]) -> list[Link]:
    """The diver's order, with an address named twice kept where it first appears - the same
    entry twice says nothing more, as a site's registry entries are folded. Compared exactly:
    a trailing slash makes another address.

    Each entry comes back with both members set, so a PATCH's `exclude_unset` dump stores the
    label too, null where none was sent."""
    seen: set[str] = set()
    kept: list[Link] = []
    for entry in entries:
        if entry.url not in seen:
            seen.add(entry.url)
            kept.append(Link.model_construct(url=entry.url, label=entry.label))
    return kept


def _canonical_or_none(entries: list[Link] | None) -> list[Link] | None:
    return None if entries is None else canonical_links(entries)


LinksWrite = Annotated[
    list[Link],
    Field(
        default_factory=list,
        max_length=LINKS_MAX,
        description="Addresses to open, in the diver's order; an address named twice is kept once",
    ),
    AfterValidator(canonical_links),
]
LinksUpdate = Annotated[
    list[Link] | None,
    Field(
        default=None,
        max_length=LINKS_MAX,
        description="Replaces the links whole, in the order given; cannot be null. Omit to leave them",
    ),
    AfterValidator(_canonical_or_none),
]
LinksRead = Annotated[
    list[LinkRead], Field(default_factory=list, description="Addresses to open, in the diver's order")
]
