"""What `GET /dive-sites/search` and the two points documents answer."""

import uuid as uuid_pkg
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from .dive_site import DiveSiteLookupItem, DiveSiteOwner, DiveSiteProposalState, DiveSiteSummary
from .geocoding import GeocodeResult


class DiveSiteSearchMatch(StrEnum):
    """How a site's name or other name matched the typed text, best first."""

    EXACT = "exact"
    PREFIX = "prefix"
    CONTAINS = "contains"
    # Only allowing for a typo.
    FUZZY = "fuzzy"


class DiveSiteSearchItem(DiveSiteLookupItem):
    """A site row of the search: the lookup row, with what a list of sites shows beside it."""

    kind: Annotated[Literal["site"], Field(description="`site`, as against a `place` row")]
    distance_m: Annotated[
        float | None,
        Field(default=None, description="Metres from the point the search was sent; null without one or a pin"),
    ]
    match: Annotated[
        DiveSiteSearchMatch | None,
        Field(
            default=None,
            description="How the name or an other name matched the text: `exact`, `prefix`, `contains` or `fuzzy`. "
            "Null without text, and on a row found by its locality alone",
        ),
    ]
    depth_from: float | None = None
    depth_to: float | None = None
    dives: Annotated[
        DiveSiteSummary | None,
        Field(default=None, description="What the reader's dives say of the site; null where it is not on their list"),
    ]
    proposal: Annotated[
        DiveSiteProposalState | None,
        Field(default=None, description="The reader's latest proposal on this site, while it waits or once declined"),
    ]


class PlaceSearchItem(GeocodeResult):
    """A place row of the search: the geocoder's answer, with how far it is and what is near it."""

    kind: Annotated[Literal["place"], Field(description="`place`, as against a `site` row")]
    distance_m: Annotated[
        float | None,
        Field(default=None, description="Metres from the point the search was sent to the place's centre"),
    ]
    sites_nearby: Annotated[
        int,
        Field(
            description="Sites the reader can see inside the place's box, or within 25 km of its centre "
            "where it has none"
        ),
    ]


SearchRow = Annotated[DiveSiteSearchItem | PlaceSearchItem, Field(discriminator="kind")]


class DiveSiteSearchPage(BaseModel):
    rows: list[SearchRow]
    has_more: Annotated[bool, Field(description="Whether site rows remain past this page")]
    total_count: Annotated[
        int | None,
        Field(default=None, description="With `my`, the number of site rows the filters admit; null otherwise"),
    ]
    page: int
    items_per_page: int


# Compact on purpose: one array per site rather than an object, since every session reads the
# whole Commons document.
DiveSitePoint = tuple[float, float, uuid_pkg.UUID, str, DiveSiteOwner, str | None]
CommonsDiveSitePoint = tuple[float, float, uuid_pkg.UUID, str, str]


class DiveSitePoints(BaseModel):
    points: Annotated[
        list[DiveSitePoint],
        Field(
            description="`[longitude, latitude, uuid, name, owner, proposal]` for each site on the reader's list with a"
            " pin, where `proposal` is the status of the reader's latest proposal on the site while it waits or once"
            " declined (`waiting` or `declined`), and null otherwise"
        ),
    ]


class CommonsDiveSitePoints(BaseModel):
    version: Annotated[str, Field(description="Moves whenever a Commons record is added, changed or reviewed")]
    sources: Annotated[
        dict[str, str], Field(description="The credit each source asks for, by the key the points carry")
    ]
    points: Annotated[
        list[CommonsDiveSitePoint],
        Field(description="`[longitude, latitude, uuid, name, source]` for each Commons record with a pin"),
    ]
