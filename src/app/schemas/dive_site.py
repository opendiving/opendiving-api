import uuid as uuid_pkg
from datetime import datetime
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..core.schemas import NOTES_MAX_LENGTH, PublicUUIDSchema, RejectsExplicitNulls

Latitude = Annotated[float | None, Field(default=None, ge=-90, le=90, examples=[27.8506])]
Longitude = Annotated[float | None, Field(default=None, ge=-180, le=180, examples=[34.3136])]

COORDINATE_PAIR_MESSAGE = "latitude and longitude must be set together"


class DiveSiteBase(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=255, examples=["Blue Hole"])]
    location: Annotated[str | None, Field(default=None, max_length=255, examples=["Koh Tao, Thailand"])]
    latitude: Latitude
    longitude: Longitude
    notes: Annotated[str, Field(default="", max_length=NOTES_MAX_LENGTH)]


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


class DiveSiteRead(DiveSiteBase, PublicUUIDSchema):
    """Public representation of a dive site, keyed by its opaque `uuid` rather than the
    sequential internal `id` (which is never exposed over the API).
    """

    user_uuid: uuid_pkg.UUID
    created_at: datetime


class DiveSiteReadInternal(DiveSiteBase, PublicUUIDSchema):
    """Mirrors the actual `dive_site` table columns (integer PK/FK), for server-side
    lookups only - never returned directly over the API (use `DiveSiteRead` for the
    public shape, which additionally resolves `user_id` to the owning user's `uuid`).
    """

    id: int
    user_id: int
    created_at: datetime


class DiveSiteCreate(DiveSiteBase, WholeCoordinatePair):
    model_config = ConfigDict(extra="forbid")


class DiveSiteCreateInternal(DiveSiteBase, WholeCoordinatePair):
    model_config = ConfigDict(extra="forbid")

    user_id: int


class DiveSiteUpdate(WholeCoordinatePair, RejectsExplicitNulls):
    model_config = ConfigDict(extra="forbid")

    # `location` is genuinely nullable and stays off this list: clearing it is how a site
    # entered with the wrong location gets corrected back to "not recorded", and
    # `patch_dive_site` reads that explicit null through `model_fields_set`.
    #
    # So are the coordinates, but they answer to `WholeCoordinatePair` above instead:
    # both columns are nullable, and clearing the position means sending *both* as null.
    # Listing them here would refuse that - a site whose position was mistyped could
    # never be corrected back to "not recorded".
    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ("name", "notes")

    name: Annotated[str | None, Field(min_length=1, max_length=255, default=None)]
    location: Annotated[str | None, Field(default=None, max_length=255, examples=["Koh Tao, Thailand"])]
    notes: Annotated[str | None, Field(default=None, max_length=NOTES_MAX_LENGTH)]


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
    internally as its stable key, but the field a client writes this into is an ordinary text
    input whose own example is `Koh Tao, Thailand`; putting `EG` on the wire invites it into
    a Location field or a menu hint, which is wrong on both.

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
