"""A place, as the geocoder described it when the diver picked it - or as they typed it.

One object with one pair of names, hosted by a dive site and by a part of a trip alike
(DiveJSON §6.9). It lives in its own module because both hosts reference it and neither
owns it: putting it in either one would make the other import a sibling resource's schema
for a shape that is not about that resource at all.

**Two names, and only the short one is ever rendered.** `name` is the place as a person
writes it - the name alone (`"Moalboal"`), or the name with its country
(`"Dahab, Egypt"`). `full_name` is the fullest written form the lookup returned
(`"Dahab, South Sinai Governorate, Egypt"`), stored so an export carries what the source
held and read by nothing on screen. Nothing binds the two: a lookup asked about a local
name often answers with the district around it, so `"Sipadan Island Park"` may carry
`"Sabah, Malaysia"` - shorter, and not containing it.

**A place is a value object with no identity**, so a write replaces the stored one
wholesale rather than merging into it, and clearing it is an explicit null. It is
snapshotted rather than resolved: nothing here looks a name up on the way in, which is
what keeps a throttled provider from blocking a save.

**A locality's position is not its host's.** A dive site carries its own pin as well, and
the two are different facts - the entry point against the town the geocoder resolved. The
columns are named apart on `dive_site` for that reason, and nothing fills either from the
other.
"""

from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

# §6.9's own bounds, and the widths of the columns behind them.
LOCATION_NAME_MAX = 255
LOCATION_FULL_NAME_MAX = 512

Latitude = Annotated[float | None, Field(default=None, ge=-90, le=90, examples=[27.8506])]
Longitude = Annotated[float | None, Field(default=None, ge=-180, le=180, examples=[34.3136])]

COORDINATE_PAIR_MESSAGE = "latitude and longitude must be set together"
BBOX_MESSAGE = "bbox_south, bbox_north, bbox_west and bbox_east must be set together"
BBOX_NEEDS_COORDINATES_MESSAGE = "a bounding box needs latitude and longitude"
BBOX_ORDER_MESSAGE = "bbox_south must be less than or equal to bbox_north"

#: The members a place carries, in the order the columns behind it are declared. Both hosts
#: store them flat - a dive site under a `location_` prefix beside its own pin, a trip part
#: bare - so one tuple drives the mapping in both directions.
LOCATION_FIELDS = (
    "name",
    "full_name",
    "latitude",
    "longitude",
    "bbox_south",
    "bbox_north",
    "bbox_west",
    "bbox_east",
)

#: What `dive_site`'s locality columns are called, against the site's own `name`,
#: `latitude` and `longitude`. A trip part has no place of its own to be confused with, so
#: its columns carry no prefix.
DIVE_SITE_LOCATION_PREFIX = "location_"


class WholeCoordinatePair(BaseModel):
    """Rejects half a position on the way in - a latitude without a longitude is not a
    partial position, it is a meaningless one.

    Two conditions, because a PATCH can produce a half pair two ways: **naming** one
    coordinate and not the other (`{"latitude": 27.7}` writes one column and leaves the
    stale other), or naming both with only one **value** (`{"latitude": 27.7,
    "longitude": null}`). Sending the pair or nothing keeps a whole row whole without the
    route ever reading the stored one - which also means two concurrent PATCHes cannot
    interleave into a half pair the way a read-then-compare check would allow.

    The rule lives on the *write* schemas only - every application path in, the admin
    panel included, goes through one of them, so only raw SQL can put a half pair in the
    table. That is reason enough to keep it off the read schemas: a row like that should
    read back as half a position rather than turn every read of it into a 500.
    """

    latitude: Latitude
    longitude: Longitude

    @model_validator(mode="after")
    def _coordinates_are_a_pair(self) -> WholeCoordinatePair:
        if len({"latitude", "longitude"} & self.model_fields_set) == 1:
            raise ValueError(COORDINATE_PAIR_MESSAGE)
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError(COORDINATE_PAIR_MESSAGE)
        return self


class LocationInput(WholeCoordinatePair):
    """A place on the way in, for either host.

    A place the geocoder could not answer for arrives as a bare `name`, which is the
    free-text escape hatch that keeps a throttled provider from blocking a save. A
    reverse-geocoded place arrives the same way on purpose: the coordinates a pin returns
    are the *host's* position, not the locality's, and filling `latitude`/`longitude` from
    them would claim the town sits exactly where the diver dropped the marker.
    """

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=LOCATION_NAME_MAX, examples=["Dahab, Egypt"])]
    full_name: Annotated[
        str | None,
        Field(default=None, max_length=LOCATION_FULL_NAME_MAX, examples=["Dahab, South Sinai Governorate, Egypt"]),
    ]
    bbox_south: Annotated[float | None, Field(default=None, ge=-90, le=90, examples=[9.89])]
    bbox_north: Annotated[float | None, Field(default=None, ge=-90, le=90, examples=[9.98])]
    bbox_west: Annotated[float | None, Field(default=None, ge=-180, le=180, examples=[123.35])]
    bbox_east: Annotated[float | None, Field(default=None, ge=-180, le=180, examples=[123.44])]

    @model_validator(mode="after")
    def _bounding_box_is_whole(self) -> LocationInput:
        corners = (self.bbox_south, self.bbox_north, self.bbox_west, self.bbox_east)
        if any(corner is not None for corner in corners):
            if not all(corner is not None for corner in corners):
                raise ValueError(BBOX_MESSAGE)
            if self.latitude is None or self.longitude is None:
                raise ValueError(BBOX_NEEDS_COORDINATES_MESSAGE)
            # Only the north/south pair is ordered. West > east is a legitimate box that
            # crosses the antimeridian, and Nominatim returns those for real places -
            # rejecting it would refuse to record Fiji or the Chukchi Sea.
            if self.bbox_south is not None and self.bbox_north is not None and self.bbox_south > self.bbox_north:
                raise ValueError(BBOX_ORDER_MESSAGE)
        return self


class LocationRead(BaseModel):
    """Public shape of a place - no id of its own, because there is nothing to address it
    by: it is replaced wholesale with whatever holds it.
    """

    name: str
    full_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    bbox_south: float | None = None
    bbox_north: float | None = None
    bbox_west: float | None = None
    bbox_east: float | None = None


def location_columns(location: LocationInput | None, prefix: str = "") -> dict[str, Any]:
    """A place spread across its host's columns, every one of them named.

    All eight are always present, as `None` where there is no place: a partial mapping
    would leave a cleared locality's stale centre behind on an update, and the trip part
    writer hands these to one `executemany` whose column list comes from the first row.
    """
    return {f"{prefix}{field}": None if location is None else getattr(location, field) for field in LOCATION_FIELDS}


def location_from_row(row: Any, prefix: str = "") -> LocationRead | None:
    """A place read back off a row, or `None` where the row holds none.

    `name` is what says whether there is a place at all: it is the one member a location
    must have, so a row with none never had one.

    Takes a result row, a model instance or a plain mapping, because all three reach this:
    FastCRUD hands back dicts on the searched path and models on the unsearched one.
    """
    read = (lambda field: row[field]) if isinstance(row, Mapping) else (lambda field: getattr(row, field))
    if read(f"{prefix}name") is None:
        return None
    return LocationRead(**{field: read(f"{prefix}{field}") for field in LOCATION_FIELDS})
