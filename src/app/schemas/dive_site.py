import uuid as uuid_pkg
from datetime import datetime
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..core.schemas import NOTES_MAX_LENGTH, PublicUUIDSchema, RejectsExplicitNulls
from .location import (
    LOCATION_FULL_NAME_MAX,
    LOCATION_NAME_MAX,
    Latitude,
    LocationInput,
    LocationRead,
    Longitude,
    WholeCoordinatePair,
)


class DiveSiteBase(BaseModel):
    """What every shape of a dive site carries apart from its locality.

    The locality is split out because the wire nests it and the table does not: a
    `DiveSiteRead` carries one `location` object, while `DiveSiteReadInternal` mirrors the
    eight `location_*` columns behind it so FastCRUD can select them by name.
    """

    name: Annotated[str, Field(min_length=1, max_length=255, examples=["Blue Hole"])]
    latitude: Latitude
    longitude: Longitude
    notes: Annotated[str, Field(default="", max_length=NOTES_MAX_LENGTH)]


class DiveSiteLocationColumns(BaseModel):
    """The locality as `dive_site` stores it: flat, prefixed, beside the site's own pin.

    Never on the wire. The prefix is what keeps the two positions apart in the table - a
    site's `latitude` is the pin a diver dropped, `location_latitude` is the centre of the
    town the geocoder resolved, and nothing fills either from the other.

    **Permissive on purpose**, which is why the write shapes take the subclass below
    instead: this is what `DiveSiteReadInternal` mirrors the table with, and a row that
    only raw SQL could have written - an empty locality name, say - should read back as
    the odd thing it is rather than turn every read of that site into a 500. Same split,
    and the same reason, as `WholeCoordinatePair` not being on `DiveSiteBase`.
    """

    location_name: Annotated[str | None, Field(default=None, max_length=LOCATION_NAME_MAX)]
    location_full_name: Annotated[str | None, Field(default=None, max_length=LOCATION_FULL_NAME_MAX)]
    location_latitude: Latitude
    location_longitude: Longitude
    location_bbox_south: Annotated[float | None, Field(default=None, ge=-90, le=90)]
    location_bbox_north: Annotated[float | None, Field(default=None, ge=-90, le=90)]
    location_bbox_west: Annotated[float | None, Field(default=None, ge=-180, le=180)]
    location_bbox_east: Annotated[float | None, Field(default=None, ge=-180, le=180)]


class DiveSiteLocationColumnsInput(DiveSiteLocationColumns):
    """The same columns on the way in, where an empty locality name is refused.

    §6.9 makes a place's `name` 1-255, so `""` is not a short name but a place with none:
    it exports a `location.name` the format's schema rejects and reads back as a nameless
    place. `LocationInput` already refuses it for every API caller; this closes the same
    hole on the admin panel's own form, which writes these columns directly. The
    migration's `nullif(location, '')` clears the rows the old unbounded field left behind,
    and this is what stops a new one arriving.

    Clearing the locality is untouched: `None` is still how a site entered with the wrong
    place is corrected back to "not recorded", and a minimum length says nothing about it.
    """

    location_name: Annotated[str | None, Field(default=None, min_length=1, max_length=LOCATION_NAME_MAX)]


class DiveSiteRead(DiveSiteBase, PublicUUIDSchema):
    """Public representation of a dive site, keyed by its opaque `uuid` rather than the
    sequential internal `id` (which is never exposed over the API).
    """

    location: LocationRead | None = None
    user_uuid: uuid_pkg.UUID
    created_at: datetime


class DiveSiteReadInternal(DiveSiteBase, DiveSiteLocationColumns, PublicUUIDSchema):
    """Mirrors the actual `dive_site` table columns (integer PK/FK), for server-side
    lookups only - never returned directly over the API (use `DiveSiteRead` for the
    public shape, which additionally resolves `user_id` to the owning user's `uuid` and
    nests the locality).
    """

    id: int
    user_id: int
    created_at: datetime


class DiveSiteCreate(DiveSiteBase, WholeCoordinatePair):
    model_config = ConfigDict(extra="forbid")

    location: LocationInput | None = None


class DiveSiteCreateInternal(DiveSiteBase, DiveSiteLocationColumnsInput, WholeCoordinatePair):
    """What reaches FastCRUD, so the locality is flat here where `DiveSiteCreate` nests it."""

    model_config = ConfigDict(extra="forbid")

    user_id: int


class _DiveSiteUpdateFields(WholeCoordinatePair, RejectsExplicitNulls):
    """What both update shapes carry, which is everything but the locality.

    The two differ only in how they spell a place: `DiveSiteUpdate` is column-shaped for
    CRUDAdmin and the `NOT NULL` sweep, `DiveSiteUpdateRequest` nests it for the API.
    """

    # The locality is genuinely nullable and stays off this list: clearing it is how a
    # site entered with the wrong one gets corrected back to "not recorded", and
    # `patch_dive_site` reads that explicit null through `model_fields_set`.
    #
    # So are the coordinates, but they answer to `WholeCoordinatePair` above instead:
    # both columns are nullable, and clearing the position means sending *both* as null.
    # Listing them here would refuse that - a site whose position was mistyped could
    # never be corrected back to "not recorded".
    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ("name", "notes")

    name: Annotated[str | None, Field(min_length=1, max_length=255, default=None)]
    notes: Annotated[str | None, Field(default=None, max_length=NOTES_MAX_LENGTH)]


class DiveSiteUpdate(_DiveSiteUpdateFields, DiveSiteLocationColumnsInput):
    """CRUDAdmin's Dive Site form, and the shape `test_update_explicit_nulls.py` sweeps
    against the `dive_site` table's columns - so the locality is flat here, as the table
    has it. `TripUpdate` is split from `TripUpdateRequest` for the same reason.
    """

    model_config = ConfigDict(extra="forbid")


class DiveSiteUpdateRequest(_DiveSiteUpdateFields):
    """Request body for `PATCH /dive-site/{uuid}`.

    Naming `location` **replaces** the stored place wholesale rather than merging into it:
    it is a value object with no identity, so there is nothing to merge into, and a
    partial update would leave a cleared locality's centre and box behind. An explicit
    null clears it.
    """

    model_config = ConfigDict(extra="forbid")

    location: LocationInput | None = None


class DiveSiteUpdateInternal(DiveSiteUpdate):
    updated_at: datetime


# -------------- catalog suggestions --------------
# Deliberately not a `DiveSiteRead`. A suggestion is not a resource: it has no uuid, no
# owner and no `created_at`, nothing downstream can reference it, and picking one copies
# values into a per-user row rather than linking to anything. See
# `services.dive_site_catalog`.

# The source that supplied a suggestion. Both are databases with their own licences, which
# is why every result carries an `attribution` naming its own.
DiveSiteSuggestionSource = Literal["osm", "wikidata"]


class DiveSiteSuggestion(BaseModel):
    """One hit from `GET /dive-sites/suggest`.

    `name` is what the site is called where it is, and is what a client fills the Name field
    from; `name_en` exists so a Latin keyboard reaches 砂辺 by typing "Sunabe", and is null
    wherever the two would say the same thing. OSM's own semantics make `name` the primary.

    **All three of `name_en`, `country` and `region` are genuinely nullable**, and a client
    that assumes otherwise will be wrong on real rows: a few dozen records sit far enough
    offshore that no administrative boundary is within 50 km of them, and they ship anyway,
    because a site with no country is still a site.

    **There is no country code here, and that is deliberate.** The catalog carries one
    internally as its stable key, but the field a client writes this into is a place's own
    `name`, an ordinary text input whose example is `Dahab, Egypt`; putting `EG` on the wire
    invites it into a Location field or a menu hint, which is wrong on both.

    **There is no distance either**, for a reason that is a decision rather than an omission.
    The endpoint ranks by distance when it is given a position, but the web client already
    carries `haversineMeters` and a `formatDistance` wired to the diver's unit preference, so
    it computes and formats the hint from coordinates it has. A distance on the wire would
    either duplicate that or - pre-formatted, or metric-only - silently break the imperial
    preference.
    """

    name: Annotated[str, Field(max_length=255, examples=["SS Thistlegorm"])]
    name_en: Annotated[str | None, Field(default=None, max_length=255, examples=["Sunabe"])]
    latitude: Annotated[float, Field(ge=-90, le=90, examples=[27.814092])]
    longitude: Annotated[float, Field(ge=-180, le=180, examples=[33.920048])]
    # English display names, resolved from Natural Earth when the file was built - neither
    # upstream carries them. `region` is the finer of the two and is what pulls two
    # same-named sites in one country apart.
    country: Annotated[str | None, Field(default=None, max_length=255, examples=["Egypt"])]
    region: Annotated[str | None, Field(default=None, max_length=255, examples=["South Sinai"])]
    source: Annotated[DiveSiteSuggestionSource, Field(examples=["osm"])]
    source_id: Annotated[
        str,
        Field(max_length=64, examples=["node/255316037"], description="The stable identifier upstream"),
    ]
    # Carried per-result rather than in an envelope, for the reason `GeocodeResult` does it:
    # attribution is a licence condition of the data itself, so it travels with the row it
    # describes. A wire format, `[label](href)`, not display copy - the clients collapse
    # repeats by string and render one credit line. An OSM row and a Wikidata row carry
    # different strings, and the OSM one is byte-identical to the geocoder's so that a menu
    # showing both sources still shows one OpenStreetMap credit.
    attribution: Annotated[
        str,
        Field(
            max_length=255,
            examples=["[Data © OpenStreetMap contributors, ODbL 1.0.](https://osm.org/copyright)"],
        ),
    ]


class DiveSiteSuggestResponse(BaseModel):
    """A capped list, not a paginated envelope.

    A picker feed like `GET /species/search` and `GET /geocode/search`, not a browsable
    collection: there is no page to ask for, and a total over a suggestion catalog is not a
    number anyone acts on. `has_more` is the one thing a client needs - it renders "keep
    typing to narrow" and stops.
    """

    results: Annotated[list[DiveSiteSuggestion], Field(default_factory=list)]
    has_more: Annotated[bool, Field(default=False, description="True when matches were cut by the result cap")]
