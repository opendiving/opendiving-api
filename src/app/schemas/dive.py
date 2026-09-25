import uuid as uuid_pkg
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, ClassVar, Literal, Self

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from ..core.schemas import (
    NOTES_MAX_LENGTH,
    PublicUUIDSchema,
    RejectsExplicitNulls,
    StoredVocabulary,
)
from ..core.utils.datetime_offset import full_date_is_a_date, require_utc_offset
from .dive_mixture import DiveMixtureCreate, DiveMixtureRead
from .dive_profile import DiveProfileInfo
from .gear_item import GearItemInfo
from .location import Latitude, LocationRead, Longitude

_START_TIME_EXAMPLE = "2021-04-04T10:04:47.910+02:00"
_LOCAL_START_TIME_EXAMPLE = "2021-04-04T10:04:47.910"
_DATE_ONLY_START_TIME_EXAMPLE = "2021-04-04"

# `start_time` carries an explicit UTC offset wherever a dive is **created**: it's the
# offset the caller (e.g. the web app, defaulting to the browser's own offset) knows the
# dive happened in, and a naive datetime is rejected rather than silently assumed to be UTC
# or local. Manual entry and the dive-computer parse path both know one, so nothing that
# creates a dive through these schemas ever has to guess.
DiveStartTime = Annotated[datetime, AfterValidator(require_utc_offset)]

# The permissive counterpart, which has to serve what is *stored* rather than what a create
# demands. A dive whose `utc_offset_minutes` is NULL records a wall clock with an unknown
# instant (DiveJSON spec §5.2), and `combine_start_time` reconstructs it naive - so a read
# schema carrying `DiveStartTime` would 500 on the very row the logbook importer exists to
# be able to accept. A dive whose time of day is unknown is a bare `date`, and
# `full_date_is_a_date` keeps Pydantic from widening `"2021-04-04"` to a midnight on the way
# back in - which a cached response re-validated against its `response_model` would do.
#
# It is on one **write** shape too, `DiveUpdate`, and that is not a read spelling leaking
# across: an update may preserve an unknown offset or an unknown time of day but not remove
# either, and which case a given body is depends on the dive being updated - something no
# schema can see. `split_updated_start_time` holds that half of the rule, so the two names no
# longer say on their own which side of the API a field is on.
DiveLocalStartTime = Annotated[datetime | date, BeforeValidator(full_date_is_a_date)]

DEPTH_PAIR_MESSAGE = "avg_depth cannot be greater than max_depth"


def validate_depth_pair(avg_depth: float | None, max_depth: float | None) -> None:
    """The one place the depth pair's ordering is decided.

    Shared by `DiveBase`/`DiveUpdate` and by `patch_dive`, which has to re-run it on a
    merged stored+incoming pair - the case an update schema cannot see, since a PATCH may
    carry either depth alone. `ck_dive_avg_depth_within_max` is underneath all three, so
    this is about answering with a sentence naming the fields rather than with an
    `IntegrityError`; the same division of labour `validate_date_range` has on courses.

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
    """What the water was - a fact about the dive, which a diver knows and types.

    A closed vocabulary rather than free text, on the same terms as `GearType`
    (`schemas/gear_item.py`): the value exists to be compared across dives, and the
    members are declared in the order a picker should list them rather than
    alphabetically, so the frontend takes that order from here instead of keeping a
    second sorted list. Deliberately **not** mirrored by a DB `CHECK` - see DECISIONS.md.

    Salt and fresh are the two real answers; brackish is a genuine third (the Baltic,
    estuaries, cenote haloclines) and is in Subsurface's vocabulary too. The density a
    computer was set to is not here: that is `Salinity`, on the recording.

    No `OTHER`: `None` already means "not recorded".
    """

    SALT = "salt"
    FRESH = "fresh"
    BRACKISH = "brackish"


class Salinity(StrEnum):
    """The water density one **device** divided pressure by to show a depth (spec §6.4a).

    A setting of the computer, like `DiveMode`, and so the recording's rather than the dive's:
    two computers on one dive can be set differently, and `EN13319` - the European depth-gauge
    calibration a Shearwater ships set to and a FIT file records - is not a kind of water. The
    dive's `water_type` is never derived from it, nor the reverse. Values as a computer offers
    them, in the format's order; no DB `CHECK`, on `WaterType`'s terms.
    """

    FRESH = "fresh"
    EN13319 = "en13319"
    SALT = "salt"


class DiveMode(StrEnum):
    """The mode one **device** ran in - never the dive's kind, which is a different claim.

    A closed vocabulary on `WaterType`'s terms and with no DB `CHECK` behind it for the same
    reason (see *"`GearItem.type` is a closed vocabulary, but has no DB `CHECK` constraint"*
    in DECISIONS.md): the enum is the write boundary, `StoredVocabulary` is how the column
    is read back.

    **It lives on the recording rather than on the dive**, which is what makes it a mode and
    not a genre. A backup computer run in gauge mode beside a primary on open circuit is
    ordinary practice and the dive was not a gauge dive; two computers give two answers, and
    the recording is the row that can hold both. A dive-level mode is the diver's own
    statement about the dive and nothing in this app writes one - it arrives with freediving
    as a product, not here.

    No `OTHER`, and no default: `None` means the file did not record one, and a reader must
    never read that as open circuit however a source format's documentation glosses an
    absence. UDDF says an absent `<divemode>` means open circuit; that is the *format*'s
    claim about its own default rather than the device's about the dive, so this app does not
    read it.
    """

    OPEN_CIRCUIT = "open_circuit"
    CLOSED_CIRCUIT = "closed_circuit"
    SEMI_CLOSED = "semi_closed"
    GAUGE = "gauge"
    FREEDIVE = "freedive"


class DecoAlgorithm(StrEnum):
    """The **family** a decompression model belongs to - not the product.

    Two values, because two are what files in hand name: a UDDF `<decomodel><buehlmann>`
    element and a FIT `dive_settings.model` of `zhl_16c` say Bühlmann, and Suunto's own
    exports name a Fused RGBM product. A family is a claim about the mathematics, so nothing
    here derives one from a product string it has not seen: a `Suunto Fused RGBM 2` is
    `RGBM` because a mapping table says that exact string is, and an unrecognized string
    fills `deco_name` and leaves this absent.

    Closed like `WaterType` and `DiveMode`, with no DB `CHECK` for the same reason. It grows
    when a file arrives naming another family - VPM and DCIEM are the two libdivecomputer
    knows and no export in hand carries either.
    """

    BUHLMANN = "buhlmann"
    RGBM = "rgbm"


class DiveBase(BaseModel):
    """Shared by the read shapes and the write ones, which is why `start_time` is the
    permissive spelling here and `DiveCreate` re-declares it as the strict one."""

    dive_number: Annotated[int, Field(examples=[5])]
    start_time: Annotated[
        DiveLocalStartTime,
        Field(
            examples=[_START_TIME_EXAMPLE, _LOCAL_START_TIME_EXAMPLE, _DATE_ONLY_START_TIME_EXAMPLE],
            description="The dive's own start time, in the timezone it was logged in. Carries no offset on a dive "
            "whose source never recorded one - the wall clock is the record and the instant is unknown. A bare date "
            "on a dive whose source recorded the day and no time of day: never place it at midnight.",
        ),
    ]
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


class RecordingReadouts(BaseModel):
    """What one computer reported about the dive as a whole, off its own arithmetic.

    The recording's rather than the dive's (spec §6.4a): CNS and OTU depend on the algorithm
    the device ran and on the exposure it carried over, and two computers on one dive give
    two answers to every one of these. Written only by the import paths - the attach path
    for every recording's files, and logbook import - never through a form, since a typed
    value would be a guess presented as a reading. See DECISIONS.md.

    The attach path and `backfill_tech_fields` write `model_fields` by name. Logbook import
    and the export name each readout themselves, so a field added here reaches neither
    until they do.
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
            description="Ambient pressure at the surface this device measured, in bar. Display only - gas-use maths "
            "deliberately assumes 1 bar (see `services/dive_gas.py`).",
        ),
    ]


class DiveTechScalars(BaseModel):
    """The entry and exit fixes a dive computer recorded and the diver never typed.

    Its own mixin rather than fields on `DiveBase` precisely so it lands on the read
    shapes and *not* on `DiveCreate`/`DiveUpdate`: these are written only by the import
    path (`services/dive_files.py::store_recording_file`), and `DiveCreate`'s `extra="forbid"`
    then makes an attempt to set one a 422 rather than a silently accepted fiction. On the
    dive rather than the recording because the format keeps its positions there.

    A diver could in principle type a position, but nothing offers to, so these travel the
    import-owned path. That has one consequence worth stating, because it is what would
    break first if a form ever did offer them: an upload that **creates** a primary
    recording writes every field of this mixin outright, `None` included
    (`store_tech_scalars`), so it overwrites whatever these hold. Every other file of that
    recording fills instead and cannot overwrite (`fill_tech_scalars`) - but that is a
    property of which write runs, not a protection these fields have, and the condition is
    "this upload created the recording" rather than "the recording had no files"
    (`_rederive_recording` on `fresh`). Adding a hand-set position means taking it off this
    mixin, not relying on the fill or adding a special case to the outright write.

    The membership is load-bearing in the other direction too - `TECH_SCALAR_FIELDS` is
    read off `model_fields`, so a field added here is written by the import and picked up
    by `backfill_tech_fields` on its next run without either being edited.

    On `DiveRead` rather than `DiveReadWithMixtures`, unlike `recordings`/`gas_use`: those
    are kept off the list response because each costs `_cached_read_dives` an extra query,
    and these are plain columns on the row that is being selected anyway.
    """

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

    The locality is the whole place object, not a trimmed copy of it: one shape named
    `location` on the wire, wherever it appears. Its own `latitude`/`longitude` are the
    locality's centre and are a different fact from the site's pin above.
    """

    name: str
    location: LocationRead | None = None
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

    # Overrides `DiveBase.water_type`, which stays `WaterType` for the writes that base
    # validates. See *"A stored vocabulary is read back as a string"* in DECISIONS.md.
    water_type: StoredVocabulary | None = None  # type: ignore[assignment]  # widening a write base's field; see `StoredVocabulary`

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
    `utc_offset_minutes` and `start_date_only` - see `combine_dive_start_time()`), since that
    recombination only makes sense once converting to the public `DiveRead` shape.
    """

    water_type: StoredVocabulary | None = None  # type: ignore[assignment]  # widening a write base's field; see `StoredVocabulary`
    start_time: datetime  # the column, never the public spelling

    id: int
    user_id: int
    trip_id: int | None = None
    course_id: int | None = None
    utc_offset_minutes: Annotated[
        int | None,
        Field(
            description="UTC offset (minutes) start_time was originally expressed in, e.g. 120 for +02:00. Null on a "
            "dive whose source recorded no offset, where `start_time` above is the wall clock labelled UTC"
        ),
    ]
    start_date_only: Annotated[
        bool,
        Field(
            default=False,
            description="Only the day of `start_time` was recorded: it holds midnight labelled UTC, with a null offset",
        ),
    ]
    created_at: datetime


class DiveFileInfo(PublicUUIDSchema):
    """Metadata about one dive-computer export a recording was read from - never its
    bytes, which are only ever served by `GET /dive/{uuid}/file/{fid}`."""

    original_filename: str
    content_type: str
    byte_size: int
    parser_key: Annotated[str, Field(description="Identifier of the parser that read this file, e.g. `suunto_xml`")]
    updated_at: datetime | None = None


class RecordingDevice(BaseModel):
    """What recorded a dive, as its own export named it.

    The read-side twin of `ParsedDevice` (`schemas/parsed_dive.py`), and deliberately a
    separate model rather than a reuse of it: that one is a *parser's* output, carrying the
    validators that turn a file's bytes into text, and this one is six stored columns. Every
    member is nullable because no format carries all six, and a recording whose source named
    no computer at all reports `null` for the whole object rather than six nulls.

    Values are as the file wrote them, never normalized - a FIT decodes its maker to the
    lowercase `suunto` and the app's JSON writes `Suunto`. Anything comparing two devices
    folds case itself (`services/dive_recordings.py`).
    """

    brand: Annotated[str | None, Field(default=None, examples=["Suunto"], description="The maker")]
    model: Annotated[str | None, Field(default=None, examples=["Suunto Ocean"], description="The product string")]
    serial: Annotated[
        str | None,
        Field(default=None, examples=["253810000400"], description="Opaque, as the file wrote it; never parsed"),
    ]
    firmware: Annotated[str | None, Field(default=None, examples=["2.51"])]
    name: Annotated[
        str | None,
        Field(default=None, examples=["Porvoo"], description="What the computer calls itself, as its owner set it"),
    ]
    dive_number: Annotated[
        int | None,
        Field(
            default=None,
            examples=[248],
            description="The **device's** own counter, not the diver's numbering - that is the dive's `dive_number`",
        ),
    ]


class RecordingDecoModel(BaseModel):
    """The decompression model one device ran on one dive, and the settings it ran it with.

    Five stored columns rather than a channel, and the distinction is what each thing is: the
    model is one setting for the whole dive, its readouts are samples of what it computed. A
    recording with no member of this recorded reports `null` for the whole object rather than
    five nulls - `_read_deco_model` in `services/dive_recordings.py` is where that is decided,
    on `_read_device`'s terms.

    `algorithm` is `StoredVocabulary` and not `DecoAlgorithm` for the reason every read shape
    in this app widens a closed vocabulary: the column has no `CHECK`, so typing the enum here
    would assert an invariant storage declined to enforce and one unrecognized row would fail
    the whole dive read.
    """

    algorithm: Annotated[
        StoredVocabulary | None,
        Field(default=None, examples=["buhlmann"], description="The model's family, where the source named one"),
    ]
    name: Annotated[
        StoredVocabulary | None,
        Field(
            default=None,
            examples=["Suunto Fused RGBM 2"],
            description="The device's own name for its model, as the source spelled it. Free text: vendors name and "
            "version their models as they please.",
        ),
    ]
    gf_low: Annotated[
        int | None,
        Field(default=None, examples=[50], description="Whole percent. Recorded with `gf_high` or not at all"),
    ]
    gf_high: Annotated[int | None, Field(default=None, examples=[85], description="Whole percent")]
    conservatism: Annotated[
        int | None,
        Field(
            default=None,
            examples=[0],
            description="The device's own conservatism setting, on the device's own scale - Suunto's P-2 to P2. "
            "Negative values are real, and the number means nothing without `name` and the recording's device.",
        ),
    ]


class RecordingRead(RecordingReadouts):
    """One device's record of a dive: what recorded it, how it was set, when it started, what
    it reported, its files and a summary of its samples.

    Ordered by `ordinal` within a dive, and **0 is primary** - the recording a single-profile
    consumer takes, the one whose readouts the dive page and the CSV show, and the one the
    app's own UDDF export writes. Order rather than a flag, matching the published
    format: a flag every writer has to set is a value every reader has to default.

    `files` is in attach order, and there may legitimately be more than one: the same
    computer exported as JSON and again as FIT is one record of one dive in two spellings,
    each filling what the other left blank. `profile` may be present with `files` empty -
    that is what a recording logbook import created from a converted document is, and it is
    first-class rather than degenerate.
    """

    uuid: uuid_pkg.UUID
    ordinal: Annotated[int, Field(description="Position among this dive's recordings; 0 is primary")]
    device: RecordingDevice | None = None
    # **The device's, not the dive's**, for the reason this row exists: two computers on one
    # dive give two answers to both, and a backup run in gauge mode does not make the dive a
    # gauge dive. `StoredVocabulary` rather than `DiveMode` on the read side, as everywhere
    # else a closed vocabulary is published.
    mode: Annotated[
        StoredVocabulary | None,
        Field(
            default=None,
            examples=["open_circuit"],
            description="The mode this device ran in: one of `open_circuit`, `closed_circuit`, `semi_closed`, "
            "`gauge`, `freedive`. Null means the file did not record one - never assume open circuit.",
        ),
    ]
    deco_model: Annotated[
        RecordingDecoModel | None,
        Field(default=None, description="The decompression model this device ran, or null where nothing recorded one"),
    ]
    salinity: Annotated[
        StoredVocabulary | None,
        Field(
            default=None,
            examples=["en13319"],
            description="The water density this device was set to: one of `fresh`, `en13319`, `salt`. A setting of "
            "the computer, not a kind of water - the dive's `water_type` is that. Null means the file did not record "
            "one.",
        ),
    ]
    # A date-time and never a bare date, even on a dive whose own start is one (spec §6.4a).
    started_at: Annotated[
        datetime | None,
        Field(
            default=None,
            examples=[_START_TIME_EXAMPLE],
            description="This device's own start - not the dive's, which a second computer entering the water later "
            "legitimately differs from. Offset-less where the source recorded no offset, exactly as a dive's is. The "
            "profile's `times` are elapsed milliseconds from this instant. Null where nothing stated one - which on a "
            "dive whose start is a bare date means its axis counts from an unknown time that day.",
        ),
    ]
    files: Annotated[list[DiveFileInfo], Field(default_factory=list, description="In attach order")]
    profile: Annotated[
        DiveProfileInfo | None,
        Field(default=None, description="Summary of this recording's samples, or null when it has none"),
    ]
    updated_at: datetime | None = None


class RecordingUpdateRequest(BaseModel):
    """The one thing about a recording a diver changes: which of them is primary.

    Not a `RejectsExplicitNulls` subclass and not a partial-update shape, because there is
    nothing optional here: `primary` is required and must be `true`. A `false` is refused
    with a 422 rather than ignored - *something* has to be primary, so "make this one not
    primary" is not an operation, and a diver who sends it means to promote a different
    recording. Everything else about a recording is what a file said, and is not the
    diver's to retype.
    """

    model_config = ConfigDict(extra="forbid")

    primary: Annotated[
        Literal[True],
        Field(description="Move this recording to the front. Must be `true`; there is no un-primary operation."),
    ]


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
            description="What `attributed_seconds` is a fraction of, in seconds: the span the dive's profile recorded, "
            "which is "
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
        DiveLocalStartTime,
        Field(
            examples=[_START_TIME_EXAMPLE],
            description="The dive's own start time, exactly as `DiveRead` reports it - the x axis. Offset-aware, "
            "unless the dive's source recorded no offset; a bare date where it recorded no time of day",
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
    # Here rather than on `DiveRead` for the reason `recordings` below gives: on the parent
    # it would land on the paginated list and cost `_cached_read_dives` - the hottest path in
    # the app - a batched query per page for something only the detail page renders. The
    # loader is already batched (`get_species_for_dives`) for the day a list surface wants
    # species chips; move it then, don't fetch per row.
    #
    # `default_factory=list` is load-bearing rather than tidy: `user_{id}_dive:{uuid}` entries
    # live an hour and replay through this schema, so every entry written before this field
    # existed lacks the key and would fail validation on read.
    species: Annotated[list[SpeciesInfo], Field(default_factory=list)]
    # **`source_file` and `profile` are gone**, and `recordings` replaces both. A dive had
    # at most one of each while a dive had at most one record; it now has an ordered list of
    # recordings, each of which carries its own files and its own profile summary. A client
    # that wants "the" file or "the" profile takes the first recording's, which is what
    # ordinal 0 means.
    #
    # Deliberately here rather than on `DiveRead`, which `DiveReadWithMixtures` extends:
    # putting it on the parent would inherit it onto the paginated list response too,
    # adding queries to `_cached_read_dives` - the hottest path in the app - for something
    # only the detail page renders. `get_recordings_for_dives` is already batched for the
    # day a recordings marker in the list changes that.
    #
    # `default_factory=list` is load-bearing rather than tidy, for `species`' reason one
    # field up: `user_{id}_dive:{uuid}` entries live an hour and replay through this schema.
    #
    # Summaries only. The series themselves are tens of KB and are fetched separately, with
    # their own ETag, from `GET /dive/{uuid}/recording/{rid}/profile`.
    recordings: Annotated[
        list[RecordingRead],
        Field(default_factory=list, description="What recorded this dive, in order; the first is primary"),
    ]
    # Here rather than on `DiveRead` for the same reason as `recordings` above, with one
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


class DiveMergeRequest(BaseModel):
    """The two dives to fold into one.

    **A list rather than two named fields, because the two are symmetric inputs.** Which one
    survives is the server's answer, not the caller's - the earlier dive by the clock rule
    the match gates already use - and naming one of them `uuid` and the other `other_uuid`
    would imply an asymmetry the operation does not have. The response says which survived.

    Exactly two, because merging three is three decisions about which pair folds first and a
    diver who wants that can merge twice.
    """

    model_config = ConfigDict(extra="forbid")

    dive_uuids: Annotated[
        list[uuid_pkg.UUID],
        Field(
            min_length=2,
            max_length=2,
            description="The two dives to merge, in any order. Both must be the caller's own and both must have a "
            "dive-computer recording.",
        ),
    ]

    @model_validator(mode="after")
    def _two_different_dives(self) -> Self:
        """One uuid twice is a request that would soft-delete the dive it just merged into."""
        if self.dive_uuids[0] == self.dive_uuids[1]:
            raise ValueError("A dive cannot be merged with itself.")
        return self


class DiveMergeResult(BaseModel):
    """What the merge produced: the dive that survived, and the uuid that no longer resolves."""

    dive: Annotated[
        DiveReadWithMixtures,
        Field(description="The surviving dive, read back whole - its recordings, cylinders and figures as merged"),
    ]
    removed_dive_uuid: Annotated[
        uuid_pkg.UUID,
        Field(
            description="The dive that was merged away. It is soft-deleted and **not recoverable through the API**: "
            "its recordings, files, cylinders, sites, gear, species and notes are now the surviving dive's."
        ),
    ]
    folded: Annotated[
        bool,
        Field(
            description="True when the two dives turned out to be one computer's two records of one dive and were "
            "folded into a single recording, samples and all. False when they were two different computers - or a "
            "pair with no start to place on one axis - and the recordings were appended side by side instead.",
        ),
    ]


class DiveNeighbor(PublicUUIDSchema):
    """The bare minimum to link to an adjacent dive: its uuid, and enough to label the
    link. Not a `DiveRead` - the caller renders a prev/next control, not a dive, and the
    full shape would cost the enrichment queries `_cached_read_dives` does per dive.
    """

    dive_number: Annotated[int, Field(examples=[5])]
    start_time: Annotated[DiveLocalStartTime, Field(examples=[_START_TIME_EXAMPLE])]


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
    """One dive whose number a renumber would change (or did change).

    A *response* shape, so `start_time` is the permissive spelling: the request's
    `from_start_time` above still requires an offset (the caller is naming an instant),
    while a dive being renumbered may be one whose own source recorded none - and the
    strict annotation here would have surfaced that as a 500 rather than as anything a
    caller could act on.
    """

    dive_uuid: uuid_pkg.UUID
    start_time: Annotated[DiveLocalStartTime, Field(examples=[_START_TIME_EXAMPLE])]
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

    # Narrowed back from `DiveBase`'s permissive spelling: this is the create side, and a
    # caller creating a dive knows the offset it happened in (the browser reads its own
    # off `Date.getTimezoneOffset()`). Only the logbook importer, which does not come
    # through here, may write a dive with no offset at all. `DiveUpdate` is the write
    # shape this narrowing deliberately does *not* reach - it may carry that state
    # forward on a dive that already has it, never begin one.
    start_time: Annotated[DiveStartTime, Field(examples=[_START_TIME_EXAMPLE])]
    trip_uuid: Annotated[
        uuid_pkg.UUID | None, Field(default=None, description="Public id of the trip this dive belongs to")
    ]
    course_uuid: Annotated[
        uuid_pkg.UUID | None, Field(default=None, description="Public id of the training course this dive was on")
    ]


class DiveCreateInternal(DiveBase):
    model_config = ConfigDict(extra="forbid")

    start_time: datetime  # the column, never the public spelling
    user_id: int
    trip_id: int | None = None
    course_id: int | None = None
    # `start_time` on this schema is the UTC instant to store (already split from the
    # public `start_time` via `split_start_time()`), paired with the offset it was split
    # from. `None` is the importer's offset-unknown state, where the instant is really the
    # wall clock labelled UTC - see `core/utils/datetime_offset.py`.
    utc_offset_minutes: int | None


class DiveCreateRequest(DiveCreate):
    """Request body for creating a dive, including its gas mixtures, dive site(s) and gear."""

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
    # was `if values.start_time is not None`, so an explicit null skipped the split-and-store
    # branch, still reached the database from `model_dump`, and left `utc_offset_minutes`
    # describing the *previous* start time.
    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ("dive_number", "start_time", "duration", "notes")

    dive_number: Annotated[int | None, Field(examples=[5], default=None)]
    # The permissive spelling, unlike `DiveCreate`'s. An update may **preserve** an unknown
    # offset or an unknown time of day - an imported dive stays editable, and the offsetless
    # or date-only `started_at` this app exports for such a dive is a value it will take back
    # - but it may not **remove** either, which `patch_dive` refuses through
    # `split_updated_start_time`. Only the stored dive says which a given body is, so the
    # refusal cannot live here.
    start_time: Annotated[
        DiveLocalStartTime | None,
        Field(
            examples=[_START_TIME_EXAMPLE, _LOCAL_START_TIME_EXAMPLE, _DATE_ONLY_START_TIME_EXAMPLE],
            default=None,
        ),
    ]
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
