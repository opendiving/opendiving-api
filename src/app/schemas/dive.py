import uuid as uuid_pkg
from datetime import datetime
from enum import StrEnum
from typing import Annotated, ClassVar, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from ..core.schemas import NOTES_MAX_LENGTH, PublicUUIDSchema, RejectsExplicitNulls
from ..core.utils.datetime_offset import require_utc_offset
from .dive_mixture import DiveMixtureCreate, DiveMixtureRead
from .dive_profile import DiveProfileInfo
from .dive_site import Latitude, Longitude
from .gear_item import GearItemInfo

_START_TIME_EXAMPLE = "2021-04-04T10:04:47.910+02:00"

# `start_time` always carries an explicit UTC offset over the API, both ways: on input,
# it's the offset the caller (e.g. the web app, defaulting to the browser's own offset)
# knows the dive happened in; on output, it's reconstructed from the dive's stored
# `utc_offset_minutes` (see `core/utils/datetime_offset.py`) so a dive always displays in
# the timezone it was actually logged in, not the viewer's. A naive datetime (no offset)
# is rejected rather than silently assumed to be UTC or local.
DiveStartTime = Annotated[datetime, AfterValidator(require_utc_offset)]

DEPTH_PAIR_MESSAGE = "avg_depth cannot be greater than max_depth"


def validate_depth_pair(avg_depth: float | None, max_depth: float | None) -> None:
    """The one place the depth pair's ordering is decided.

    Shared by `DiveBase`/`DiveUpdate` and by `patch_dive`, which has to re-run it on a
    merged stored+incoming pair - the case an update schema cannot see, since a PATCH may
    carry either depth alone. `ck_dive_avg_depth_within_max` is underneath all three, so
    this is about answering with a sentence naming the fields rather than with an
    `IntegrityError`; the same division of labour `validate_date_range` has on trips and
    courses.

    A mean cannot exceed a maximum, so a dive that says otherwise records at least one
    wrong number - and until this landed the app accepted it and exported it, which the
    DiveJSON reference validator rejects (spec §6.2, and §3's cross-member arithmetic
    list). Equality is fine: a perfectly square profile is unusual, not impossible.

    `DiveBase` is a *read* schema as well as a write one (`DiveRead` inherits it, and
    `crud_dives` validates every stored row through `DiveReadInternal`), so this rule can
    in principle refuse a row on the way out - the liability `DiveMixtureBase`'s docstring
    is about. It is safe here for the reason it is safe on `CourseBase`: the matching
    `CheckConstraint` lands in the same change, and its migration repairs any row that
    already violated, so no such row can exist to be read.
    """
    if avg_depth is not None and max_depth is not None and avg_depth > max_depth:
        raise ValueError(DEPTH_PAIR_MESSAGE)


class WaterType(StrEnum):
    """What the diver (or their computer) was calibrated for.

    A closed vocabulary rather than free text, on the same terms as `GearType`
    (`schemas/gear_item.py`): the value exists to be compared across dives, and the
    members are declared in the order a picker should list them rather than
    alphabetically, so the frontend takes that order from here instead of keeping a
    second sorted list. Deliberately **not** mirrored by a DB `CHECK` - see DECISIONS.md.

    Salt and fresh are the two real answers; brackish is a genuine third (the Baltic,
    estuaries, cenote haloclines) and is in Subsurface's vocabulary too. `EN13319` is the
    European standard depth-instrument calibration (~1020 kg/m3), not a kind of water -
    it is here because it is what a Shearwater ships set to and what a FIT file records,
    and folding it into `SALT` on import would be the parser substituting a plausible
    value for what the file said (see `schemas/parsed_dive.py`). The diver can correct it
    on the prefilled form.

    No `OTHER`: `None` already means "not recorded".
    """

    SALT = "salt"
    FRESH = "fresh"
    BRACKISH = "brackish"
    EN13319 = "en13319"


class DiveBase(BaseModel):
    dive_number: Annotated[int, Field(examples=[5])]
    start_time: Annotated[DiveStartTime, Field(examples=[_START_TIME_EXAMPLE])]
    duration: Annotated[int, Field(examples=[2048], description="Dive duration in seconds")]

    max_depth: Annotated[float | None, Field(default=None)]
    avg_depth: Annotated[float | None, Field(default=None)]
    bottom_temperature: Annotated[float | None, Field(default=None)]
    visibility: Annotated[int | None, Field(default=None, description="Underwater visibility in meters")]
    weight: Annotated[
        float | None, Field(default=None, examples=[6.0], description="Total ballast carried, in kilograms")
    ]
    # On `DiveBase` rather than `DiveTechScalars`, so both are writable on create and
    # edit: these are things a diver knows, and an import that re-attached would otherwise
    # overwrite a correction (see that mixin's docstring). No Pydantic bounds on
    # `altitude`, matching every other numeric field here - `ck_dive_altitude_range` is
    # the bound, mirrored by the frontend's Zod schema.
    water_type: Annotated[
        WaterType | None,
        Field(default=None, examples=[WaterType.SALT], description="What the water was, as a dive computer calibrates"),
    ]
    altitude: Annotated[
        int | None,
        Field(default=None, examples=[372], description="Elevation of the water surface, in meters above sea level"),
    ]

    notes: Annotated[str, Field(default="", max_length=NOTES_MAX_LENGTH)]

    @model_validator(mode="after")
    def _check_depth_pair(self) -> Self:
        validate_depth_pair(self.avg_depth, self.max_depth)
        return self


class DiveTechScalars(BaseModel):
    """What the dive computer recorded and the diver never typed.

    Its own mixin rather than fields on `DiveBase` precisely so it lands on the read
    shapes and *not* on `DiveCreate`/`DiveUpdate`: these are written only by the import
    path (`services/dive_files.py::store_dive_file`), and `DiveCreate`'s `extra="forbid"`
    then makes an attempt to set one a 422 rather than a silently accepted fiction.

    CNS and OTU depend on the decompression algorithm the device ran and on the diver's
    exposure carried over from earlier dives, so nothing on a logged dive reconstructs
    them - a typed-in value would be a guess presented as a reading. See DECISIONS.md.

    The entry/exit coordinates join them for the *mechanism* rather than that argument: a
    diver could in principle type a position, but nothing offers to, so these travel the
    same import-owned path. That has one consequence worth stating, because it is what
    would break first if a form ever did offer them: `store_tech_scalars` writes every
    field of this mixin on every attach, `None` included, so re-attaching an export
    overwrites whatever these hold. Adding a hand-set position means taking it off this
    mixin, not adding a special case to that write.

    The membership is load-bearing in the other direction too - `TECH_SCALAR_FIELDS` is
    read off `model_fields`, so a field added here is written by the import and picked up
    by `backfill_tech_fields` on its next run without either being edited.

    On `DiveRead` rather than `DiveReadWithMixtures`, unlike `source_file`/`gas_use`/
    `profile`: those are kept off the list response because each costs
    `_cached_read_dives` an extra query, and these are plain columns on the row that is
    being selected anyway.
    """

    cns_start: Annotated[
        float | None, Field(default=None, examples=[8.0], description="CNS oxygen-toxicity clock at the start, in %")
    ]
    cns_end: Annotated[
        float | None, Field(default=None, examples=[9.0], description="CNS oxygen-toxicity clock at the end, in %")
    ]
    otu_start: Annotated[
        float | None, Field(default=None, examples=[22.0], description="Oxygen tolerance units at the start")
    ]
    otu_end: Annotated[
        float | None, Field(default=None, examples=[23.0], description="Oxygen tolerance units at the end")
    ]
    surface_pressure_bar: Annotated[
        float | None,
        Field(
            default=None,
            examples=[1.057],
            description="Ambient pressure at the surface, in bar. Display only - gas-use maths deliberately "
            "assumes 1 bar (see `services/dive_gas.py`).",
        ),
    ]
    entry_latitude: Annotated[
        float | None,
        Field(
            default=None,
            examples=[28.437455],
            description="Latitude of the last position the import recorded before the descent - a satellite fix, "
            "or the dive-start position the computer logged itself",
        ),
    ]
    entry_longitude: Annotated[
        float | None,
        Field(
            default=None,
            examples=[34.458997],
            description="Longitude of the last position the import recorded before the descent - a satellite fix, "
            "or the dive-start position the computer logged itself",
        ),
    ]
    exit_latitude: Annotated[
        float | None,
        Field(default=None, examples=[28.437480], description="Latitude of the first satellite fix after the ascent"),
    ]
    exit_longitude: Annotated[
        float | None,
        Field(default=None, examples=[34.458370], description="Longitude of the first satellite fix after the ascent"),
    ]


class DiveSiteInfo(PublicUUIDSchema):
    """Summary of a dive site visited during a dive, keyed by its public `uuid`, and
    enough of the site to place it on a map without a second request per site.

    The position is the site's own, half of a dive's location story; the other half is
    the `entry_*`/`exit_*` fix recorded by the dive computer on `DiveTechScalars`.
    """

    name: str
    location: str | None = None
    latitude: Latitude
    longitude: Longitude


class SpeciesInfo(PublicUUIDSchema):
    """Summary of a species spotted on a dive, keyed by its public `uuid`.

    Mirrors `DiveSiteInfo` above: just enough to render a row without a second request per
    species. Unlike every other summary embedded in a dive, the row behind this one belongs
    to nobody - the species catalog is global (see `models/species.py`), so two divers' dives
    embed the identical `uuid`.

    `common_name` is null whenever no source offered an English one, so every client falls
    back to `scientific_name`. `rank` rides along because a sighting is not always
    species-rank - "a moray eel" is a family, and a client that renders it as though it were
    a species is claiming an identification the diver did not make.

    `photo_sha256` is **the whole photo contract**, following the avatar precedent verbatim:
    one nullable digest answers existence, version and cache-busting at once, and no URL goes
    on the wire. Non-null means "there is a photo, and this is which one"; the client builds
    `/api/v1/species/{uuid}/photo?v=<digest prefix>` itself. It has a `default` for the same
    load-bearing reason `DiveReadWithMixtures.species` does: `user_{id}_dive:{uuid}` entries
    live an hour and replay through this schema, so every entry written before this field
    existed lacks the key and would fail validation on read.

    That replay is also the staleness this feature accepts: for up to the single-dive TTL
    after a photo lands, a cached dive still says the species has none. Bounded, self-healing,
    and the alternative is the cross-user cache sweep iteration 1 exists to defer.
    """

    scientific_name: str
    common_name: str | None = None
    rank: str
    photo_sha256: str | None = None


class DiveRead(DiveBase, DiveTechScalars, PublicUUIDSchema):
    """Public representation of a dive, keyed by its opaque `uuid` rather than the
    sequential internal `id` (which is never exposed over the API). Cross-resource
    references (owning user, trip, training course) are likewise exposed via their `uuid`.
    """

    user_uuid: uuid_pkg.UUID
    trip_uuid: Annotated[
        uuid_pkg.UUID | None, Field(default=None, description="Public id of the trip this dive belongs to")
    ]
    course_uuid: Annotated[
        uuid_pkg.UUID | None, Field(default=None, description="Public id of the training course this dive was on")
    ]
    created_at: datetime
    dive_sites: Annotated[
        list[DiveSiteInfo], Field(default_factory=list, description="Dive sites visited, in the order visited")
    ]
    # A dive records the gear items used on it, never the gear *set* they were loaded
    # from: sets are purely a form-filling shortcut and can be edited or deleted
    # afterwards without rewriting history (see `models/gear_set.py`).
    gear_items: Annotated[
        list[GearItemInfo], Field(default_factory=list, description="Gear items used, in the order listed")
    ]


class DiveReadInternal(DiveBase, DiveTechScalars, PublicUUIDSchema):
    """Mirrors the actual `dive` table columns (integer PK/FK), for server-side lookups
    only - never returned directly over the API (use `DiveRead`/`DiveReadWithMixtures`
    for the public shape, which additionally resolves `user_id`/`trip_id`/`course_id` to
    the owning user's/trip's/course's `uuid` and attaches the dive's sites).

    `start_time` here is the raw stored UTC instant (not yet re-combined with
    `utc_offset_minutes` - see `combine_start_time()`), since that recombination only
    makes sense once converting to the public `DiveRead` shape.
    """

    id: int
    user_id: int
    trip_id: int | None = None
    course_id: int | None = None
    utc_offset_minutes: Annotated[
        int, Field(description="UTC offset (minutes) start_time was originally expressed in, e.g. 120 for +02:00")
    ]
    created_at: datetime


class DiveFileInfo(PublicUUIDSchema):
    """Metadata about the dive-computer export a dive was imported from - never its
    bytes, which are only ever served by `GET /dive/{uuid}/file`."""

    original_filename: str
    content_type: str
    byte_size: int
    parser_key: Annotated[str, Field(description="Identifier of the parser that read this file, e.g. `suunto_xml`")]
    updated_at: datetime | None = None


class DiveTankGasUse(BaseModel):
    """One cylinder's consumption on a multi-cylinder dive.

    Only ever produced for a dive whose import recorded which gas was breathed when (see
    `dive_profile.gas_attribution`); the time and the mean depth here are that cylinder's
    own, not the dive's. `gas_number` is what joins this back to the `DiveMixture` it was
    computed from - the client already has the mixtures, so nothing about the gas itself
    is repeated here.
    """

    gas_number: Annotated[
        int, Field(examples=[1], description="The cylinder this describes, as `DiveMixture.gas_number` labels it")
    ]
    gas_used: Annotated[
        float, Field(examples=[1863.4], description="Gas breathed from this cylinder, in liters at surface pressure")
    ]
    rmv: Annotated[
        float,
        Field(
            examples=[14.29],
            description="Respiratory minute volume while breathing this cylinder, in liters per minute at surface "
            "pressure",
        ),
    ]
    sac_bar_per_min: Annotated[
        float, Field(examples=[1.19], description="This cylinder's own surface air consumption, in bar per minute")
    ]
    seconds_on_gas: Annotated[
        int, Field(examples=[2355], description="How long this cylinder was breathed, in seconds")
    ]
    mean_depth: Annotated[
        float,
        Field(
            examples=[24.8],
            description="Mean depth over the time this cylinder was breathed, in meters - the depth its consumption "
            "was normalized from, and not the dive's average depth",
        ),
    ]


class DiveGasUse(BaseModel):
    """Surface-normalized gas consumption for a dive, derived from its duration, average
    depth and cylinder pressures - see `services/dive_gas.py` for the arithmetic and for
    the (deliberately strict) conditions under which it's derivable at all.

    Present as a whole or not at all, rather than field-by-field: a dive either records
    enough to know what it consumed or it doesn't, and a half-populated version - litres
    used but no rate, say - would read as a number worth acting on when it isn't. Divers
    plan gas off these figures.

    The figures describe **the cylinders accounted for**, which on a single-cylinder dive
    is the dive. On a multi-cylinder dive whose import recorded which gas was breathed when,
    they are the totals over `tanks`, and `attributed_seconds`/`duration` are what
    say how much of the dive that covers - a staged deco bottle with no pressures logged
    contributes neither its gas nor its time.

    On a dive whose cylinders are *all* flagged as breathed in parallel - a sidemount pair
    or independent doubles - they are the whole dive again: the litres of every cylinder,
    over the dive's own duration and average depth. `tanks` is then empty and both seconds
    fields are null, because that derivation needs no attribution and so has none to report.
    """

    gas_used: Annotated[float, Field(examples=[1800.0], description="Gas breathed, in liters at surface pressure")]
    rmv: Annotated[
        float,
        Field(
            examples=[14.29],
            description="Respiratory minute volume: liters per minute at surface pressure. Cylinder-independent, "
            "so it's the figure to compare across dives.",
        ),
    ]
    sac_bar_per_min: Annotated[
        float | None,
        Field(
            examples=[1.19],
            description="Surface air consumption in bar per minute. Only meaningful alongside this dive's cylinder "
            "volume, but it's what a pressure gauge actually shows. **Null on a multi-cylinder dive**, where there "
            "is generally no such thing: 10 bar out of an 11 L stage and 10 bar out of a 22 L twinset are different "
            "amounts of gas. Each entry in `tanks` carries its own, which is meaningful because a tank has one "
            "volume. The one exception is a dive whose cylinders are all flagged as breathed in parallel **and are "
            "of exactly equal volume**, where this is their pooled figure - the mean drop across them per "
            "surface-minute, which is what the same pair logged as a single manifolded cylinder would report.",
        ),
    ]
    tanks: Annotated[
        list[DiveTankGasUse],
        Field(
            default_factory=list,
            description="Per-cylinder breakdown, on a dive whose import recorded which gas was breathed when. Empty "
            "on a single-cylinder dive, where the figures above already describe the one tank, and empty on a dive "
            "computed by summing a flagged parallel set, which needs no attribution and so has none to break down. "
            "May hold a single entry: a two-cylinder dive whose deco bottle logged no pressures is the commonest "
            "shape there is.",
        ),
    ]
    attributed_seconds: Annotated[
        int | None,
        Field(
            default=None,
            examples=[2355],
            description="Seconds of the dive the figures above account for, when they come from `tanks`. Null "
            "whenever the whole dive is accounted for and there is no fraction to report: a single-cylinder dive, "
            "and a flagged parallel set summed over the dive's own duration.",
        ),
    ]
    duration: Annotated[
        int | None,
        Field(
            default=None,
            examples=[4619],
            description="What `attributed_seconds` is a fraction of: the span the dive's profile recorded, which is "
            "what the attribution ran over. Deliberately not the dive's own `duration`, which is the diver's record "
            "and may have been edited - the two halves of the fraction have to come from the same place to be worth "
            "anything. Null alongside `attributed_seconds`.",
        ),
    ]


class DiveGasUsePoint(BaseModel):
    """One dive's entry in a user's gas-use history (`GET /user/gas-use-history`).

    Carries just enough of the dive to plot and label a point and to link back to it -
    not a trimmed `DiveRead`. The series exists to be graphed, and every field here is
    either an axis, a tooltip, or the link target.
    """

    dive_uuid: Annotated[uuid_pkg.UUID, Field(description="Public id of the dive this point came from")]
    dive_number: int
    start_time: Annotated[
        DiveStartTime,
        Field(
            examples=[_START_TIME_EXAMPLE],
            description="The dive's own offset-aware start time, exactly as `DiveRead` reports it - the x axis",
        ),
    ]
    avg_depth: Annotated[
        float,
        Field(
            description="The dive's own average depth, in meters - a label for the point, not necessarily the depth "
            "the figure was normalized from. On a single-cylinder dive it is both; on a multi-cylinder one each tank "
            "was normalized against its own `mean_depth`, which can be a long way from this.",
        ),
    ]
    gas_use: DiveGasUse


class DiveActivityPoint(BaseModel):
    """One calendar day of a user's diving (`GET /user/dive-activity`).

    Counts only - no dive is named here, unlike `DiveGasUsePoint`. The series answers
    "how much am I diving", which is a question about volume over time, and a day with
    four dives in it has nothing useful to say about any one of them.

    A *day* rather than a month because the client windows the same series three ways -
    day by day, month by month, year by year - and the finest bucket is the only one that
    can serve all three. Summing days into months is arithmetic the client already does to
    reach years; sending both would be the same dives counted twice, and the response
    stays proportional to the diving either way (one row per day dived, never per day).

    Discrete `year`/`month`/`day` rather than a date or a `"2026-04-12"` string, because
    that is what this is: a bucket label, not an instant. A datetime would invite a
    timezone conversion downstream and drop a day's dives into the one before it - the
    exact bug `utc_offset_minutes` exists to prevent (see `services/dive_activity.py`).
    """

    year: Annotated[int, Field(examples=[2026], description="Calendar year, in the dives' own local time")]
    month: Annotated[int, Field(ge=1, le=12, examples=[4], description="Calendar month, 1-12")]
    day: Annotated[int, Field(ge=1, le=31, examples=[12], description="Day of the month, 1-31")]
    dives: Annotated[int, Field(examples=[3], description="Dives logged on that day")]


class DiveReadWithMixtures(DiveRead):
    mixtures: Annotated[list[DiveMixtureRead], Field(default_factory=list)]
    # Here rather than on `DiveRead` for the reason `source_file` below gives: on the parent
    # it would land on the paginated list and cost `_cached_read_dives` - the hottest path in
    # the app - a batched query per page for something only the detail page renders. The
    # loader is already batched (`get_species_for_dives`) for the day a list surface wants
    # species chips; move it then, don't fetch per row.
    #
    # `default_factory=list` is load-bearing rather than tidy: `user_{id}_dive:{uuid}` entries
    # live an hour and replay through this schema, so every entry written before this field
    # existed lacks the key and would fail validation on read. Same lesson as
    # `TripRead.locations`.
    species: Annotated[list[SpeciesInfo], Field(default_factory=list)]
    # Deliberately here rather than on `DiveRead`, which `DiveReadWithMixtures` extends:
    # putting it on the parent would inherit it onto the paginated list response too,
    # adding a query to `_cached_read_dives` - the hottest path in the app - for
    # something only the detail page renders.
    source_file: Annotated[
        DiveFileInfo | None,
        Field(default=None, description="The dive-computer export this dive was imported from, if any"),
    ]
    # Here rather than on `DiveRead` for the same reason as `source_file` above, with one
    # extra: it's derived from the mixtures, which the list response doesn't carry at all.
    # Putting it on the parent would mean a batched mixture lookup in `_cached_read_dives`
    # purely to compute it.
    gas_use: Annotated[
        DiveGasUse | None,
        Field(
            default=None,
            description="Surface-normalized gas consumption, or null when the dive doesn't record enough to derive it",
        ),
    ]
    # Here rather than on `DiveRead` for the same reason as `source_file` above: on the
    # parent it would land on the paginated list and cost `_cached_read_dives` - the
    # hottest path in the app - another query per page for something only the detail page
    # renders. `get_profile_infos_for_dives` is already batched for the day that changes.
    #
    # A summary only. The series themselves are tens of KB and are fetched separately,
    # with their own ETag, from `GET /dive/{uuid}/profile`.
    profile: Annotated[
        DiveProfileInfo | None,
        Field(
            default=None,
            description="Summary of this dive's per-sample profile, or null when it has none",
        ),
    ]


class DiveNeighbor(PublicUUIDSchema):
    """The bare minimum to link to an adjacent dive: its uuid, and enough to label the
    link. Not a `DiveRead` - the caller renders a prev/next control, not a dive, and the
    full shape would cost the enrichment queries `_cached_read_dives` does per dive.
    """

    dive_number: Annotated[int, Field(examples=[5])]
    start_time: Annotated[DiveStartTime, Field(examples=[_START_TIME_EXAMPLE])]


class DiveNeighbors(BaseModel):
    """The two dives sitting either side of one dive in its owner's log, chronologically.

    `next` means *later in time*, which is the opposite end of `GET /dives`: that list is
    newest first, so this dive's `next` is the row above it there.
    """

    previous: Annotated[
        DiveNeighbor | None,
        Field(default=None, description="The dive logged immediately before this one, or null if it's the oldest"),
    ]
    next: Annotated[
        DiveNeighbor | None,
        Field(default=None, description="The dive logged immediately after this one, or null if it's the newest"),
    ]


class DiveNumberSuggestion(BaseModel):
    """What to prefill the dive number with when logging a dive at a given start time.

    Derived from the dive's *date*, not from the newest dive in the log, so back-filling
    an old dive suggests a number that belongs where that dive belongs - see
    `services/dive_numbering.py`.
    """

    dive_number: Annotated[int, Field(examples=[213], description="Suggested number for a dive at this start time")]
    is_taken: Annotated[
        bool,
        Field(
            description="Whether an existing dive already carries this number. Advisory only - the suggestion "
            "stands either way, and duplicates are a legitimate transient state while back-filling a log."
        ),
    ]


class DiveNumberingSummary(BaseModel):
    """The state of a user's dive numbering, for the log's numbering indicator.

    Reported rather than enforced: gaps mean 'part of my log lives elsewhere' as often
    as they mean 'my numbering is a mess', and only the diver knows which. See
    `services/dive_numbering.py`.
    """

    total_dives: int
    lowest: Annotated[int | None, Field(default=None, description="Lowest number in use, or null with no dives")]
    highest: Annotated[int | None, Field(default=None, description="Highest number in use, or null with no dives")]
    missing_count: Annotated[
        int, Field(description="How many numbers between `lowest` and `highest` no dive uses", examples=[34])
    ]
    duplicate_count: Annotated[
        int, Field(description="How many dives carry a number another dive also carries", examples=[2])
    ]
    out_of_date_order_count: Annotated[
        int,
        Field(description="How many dives are numbered lower than the dive that chronologically precedes them"),
    ]
    is_sequential: Annotated[
        bool,
        Field(
            description="Whether the numbers form one unbroken run with no duplicates. Note this doesn't require "
            "starting at 1: a log that begins at #47 because the first 46 dives are on paper is still sequential."
        ),
    ]


class DiveRenumberRequest(BaseModel):
    """Request body for renumbering a log.

    Always explicit - nothing in the app renumbers on its own, because a gap can be
    deliberate (see `DiveNumberingSummary`).
    """

    model_config = ConfigDict(extra="forbid")

    start_at: Annotated[
        int,
        Field(default=1, ge=1, description="Number to give the earliest dive in scope", examples=[1]),
    ]
    from_start_time: Annotated[
        DiveStartTime | None,
        Field(
            default=None,
            examples=[_START_TIME_EXAMPLE],
            description="Renumber only dives at or after this instant, leaving earlier ones untouched - so a log "
            "whose older entries mirror a paper logbook can have just its recent tail tidied. Null renumbers "
            "every dive.",
        ),
    ]
    dry_run: Annotated[
        bool,
        Field(default=False, description="Compute the changes and return them without writing anything"),
    ]


class DiveRenumberChange(BaseModel):
    """One dive whose number a renumber would change (or did change)."""

    dive_uuid: uuid_pkg.UUID
    start_time: Annotated[DiveStartTime, Field(examples=[_START_TIME_EXAMPLE])]
    dive_number: Annotated[int, Field(description="The number before the renumber", examples=[212])]
    new_dive_number: Annotated[int, Field(description="The number after it", examples=[198])]


class DiveRenumberResult(BaseModel):
    dry_run: bool
    dives_in_scope: Annotated[int, Field(description="How many dives the requested scope covers")]
    # The full list, not a sample: this is what the confirmation dialog renders, and a
    # preview that says "and 180 more" is exactly the part a diver would want to read
    # before overwriting numbers they may have written in a paper logbook. A dive log is
    # a career's worth of dives, not a dataset, and this endpoint is hit on demand.
    changes: Annotated[
        list[DiveRenumberChange],
        Field(default_factory=list, description="Every dive whose number changes, in chronological order"),
    ]


class DiveCreate(DiveBase):
    model_config = ConfigDict(extra="forbid")

    trip_uuid: Annotated[
        uuid_pkg.UUID | None, Field(default=None, description="Public id of the trip this dive belongs to")
    ]
    course_uuid: Annotated[
        uuid_pkg.UUID | None, Field(default=None, description="Public id of the training course this dive was on")
    ]


class DiveCreateInternal(DiveBase):
    model_config = ConfigDict(extra="forbid")

    user_id: int
    trip_id: int | None = None
    course_id: int | None = None
    # `start_time` on this schema is the UTC instant to store (already split from the
    # public, offset-aware `start_time` via `split_start_time()`), paired with the offset
    # it was split from.
    utc_offset_minutes: int


class DiveCreateRequest(DiveCreate):
    """Request body for creating a dive, including its gas mixtures, dive site(s) and gear."""

    user_uuid: Annotated[uuid_pkg.UUID, Field(description="Public id of the user this dive belongs to")]
    mixtures: Annotated[list[DiveMixtureCreate], Field(default_factory=list)]
    dive_site_uuids: Annotated[
        list[uuid_pkg.UUID],
        Field(default_factory=list, description="Public ids of the dive sites visited, in the order visited"),
    ]
    gear_item_uuids: Annotated[
        list[uuid_pkg.UUID],
        Field(default_factory=list, description="Public ids of the gear items used, in the order listed"),
    ]
    species_uuids: Annotated[
        list[uuid_pkg.UUID],
        Field(default_factory=list, description="Public ids of the species spotted, in the order listed"),
    ]


class DiveUpdate(RejectsExplicitNulls):
    model_config = ConfigDict(extra="forbid")

    # `dive_number`, `start_time`, `duration` and `notes` map to `NOT NULL` columns (see
    # `models/dive.py`), so an explicit null is refused by `RejectsExplicitNulls` rather
    # than reaching the driver. `start_time` was the worst of them: `patch_dive`'s guard
    # was `if values.start_time is not None`, so an explicit null skipped the
    # `split_start_time` branch, still reached the database from `model_dump`, and left
    # `utc_offset_minutes` describing the *previous* start time.
    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ("dive_number", "start_time", "duration", "notes")

    dive_number: Annotated[int | None, Field(examples=[5], default=None)]
    start_time: Annotated[DiveStartTime | None, Field(examples=[_START_TIME_EXAMPLE], default=None)]
    duration: Annotated[int | None, Field(examples=[2048], description="Dive duration in seconds", default=None)]
    max_depth: Annotated[float | None, Field(default=None)]
    avg_depth: Annotated[float | None, Field(default=None)]
    bottom_temperature: Annotated[float | None, Field(default=None)]
    visibility: Annotated[int | None, Field(default=None, description="Underwater visibility in meters")]
    weight: Annotated[float | None, Field(default=None, description="Total ballast carried, in kilograms")]
    water_type: Annotated[
        WaterType | None, Field(default=None, description="What the water was, as a dive computer calibrates")
    ]
    altitude: Annotated[
        int | None, Field(default=None, description="Elevation of the water surface, in meters above sea level")
    ]
    trip_uuid: Annotated[
        uuid_pkg.UUID | None, Field(default=None, description="Public id of the trip this dive belongs to")
    ]
    course_uuid: Annotated[
        uuid_pkg.UUID | None, Field(default=None, description="Public id of the training course this dive was on")
    ]
    notes: Annotated[
        str | None,
        Field(
            max_length=NOTES_MAX_LENGTH,
            examples=["This is my updated dive notes."],
            default=None,
        ),
    ]

    @model_validator(mode="after")
    def _check_depth_pair(self) -> Self:
        """Only sees a PATCH that carries *both* depths; `patch_dive` re-runs the rule
        against the merged stored pair for the one that carries either alone."""
        validate_depth_pair(self.avg_depth, self.max_depth)
        return self


class DiveUpdateRequest(DiveUpdate):
    """Request body for updating a dive, including replacing its gas mixtures, dive site(s)
    and gear.

    If `mixtures`/`dive_site_uuids`/`gear_item_uuids`/`species_uuids` is omitted, the
    existing mixtures/dive sites/gear/species are left untouched. If provided (even as an
    empty list), the existing ones are replaced with the given list.
    """

    mixtures: Annotated[list[DiveMixtureCreate] | None, Field(default=None)]
    dive_site_uuids: Annotated[
        list[uuid_pkg.UUID] | None,
        Field(default=None, description="Public ids of the dive sites visited, in the order visited"),
    ]
    gear_item_uuids: Annotated[
        list[uuid_pkg.UUID] | None,
        Field(default=None, description="Public ids of the gear items used, in the order listed"),
    ]
    species_uuids: Annotated[
        list[uuid_pkg.UUID] | None,
        Field(default=None, description="Public ids of the species spotted, in the order listed"),
    ]


class DiveUpdateInternal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dive_number: Annotated[int | None, Field(examples=[5], default=None)]
    start_time: Annotated[datetime | None, Field(examples=[datetime.now()], default=None)]
    duration: Annotated[int | None, Field(examples=[2048], description="Dive duration in seconds", default=None)]
    max_depth: Annotated[float | None, Field(default=None)]
    avg_depth: Annotated[float | None, Field(default=None)]
    bottom_temperature: Annotated[float | None, Field(default=None)]
    visibility: Annotated[int | None, Field(default=None, description="Underwater visibility in meters")]
    weight: Annotated[float | None, Field(default=None, description="Total ballast carried, in kilograms")]
    water_type: Annotated[
        WaterType | None, Field(default=None, description="What the water was, as a dive computer calibrates")
    ]
    altitude: Annotated[
        int | None, Field(default=None, description="Elevation of the water surface, in meters above sea level")
    ]
    trip_id: Annotated[int | None, Field(default=None, description="Internal id of the trip this dive belongs to")]
    course_id: Annotated[
        int | None, Field(default=None, description="Internal id of the training course this dive was on")
    ]
    utc_offset_minutes: Annotated[int | None, Field(default=None)]
    notes: Annotated[
        str | None,
        Field(
            max_length=NOTES_MAX_LENGTH,
            examples=["This is my updated dive notes."],
            default=None,
        ),
    ]
    updated_at: datetime


class DiveDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_deleted: bool
    deleted_at: datetime
