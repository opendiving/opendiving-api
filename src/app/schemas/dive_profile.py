"""Wire shapes for a dive's per-sample profile.

Deliberately its own module rather than an addition to `parsed_dive.py`: that one is the
`POST /dive/parse` contract, and a profile never travels over it. A profile is thousands
of readings the browser has no use for while filling in a form, and it would have to be
posted back to be stored - which would make the stored samples client-supplied and
reopen the exact trust problem the parse token exists to close.

That rule is about `/dive/parse` and is unchanged. **Logbook import is the one path that
does store client-supplied samples**, because bringing a whole logbook into your own
account is exactly what it is for - and that logbook may be a UDDF file or a `.ssrf` the
route converted, rather than anything this app wrote. `DECISIONS.md`, *"Importing a
logbook is the one client-supplied profile"*, records why that is a different question
from posting a parse back, and why the answer does not rest on who produced the document.

One shape here is not a wire shape: `GasAttribution` is what a summary *column* holds. It
lives here rather than in the service because it is read back out of JSONB and wants
validating on the way in, which is what the models in this package are for. `DepthSilhouette`
is both.
"""

import uuid as uuid_pkg
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field

# The profile axis's unit: a series' `times`, a profile's `duration` and an event's `time` are
# milliseconds (DiveJSON §5.1), and every consumer that wants seconds divides by this. The
# `ndl` and `tts` *values* stay seconds - they are readings, not positions on the axis.
MILLISECONDS_PER_SECOND = 1000

# The integer scales every readings array is expressed in - `v` in the stored shape, `values`
# on the wire and in a document. They live here (and, mirrored, in
# the frontend's `PROFILE_CHANNELS`) rather than travelling per-response: they are part
# of the format, not of a particular dive. Integers rather than floats because a float
# round-trip reintroduces `20.600000000000023`-class noise several thousand times per
# dive, and all three resolutions comfortably exceed any dive computer's real precision.
DEPTH_SCALE = 100  # centimeters
TEMPERATURE_SCALE = 10  # tenths of a degree Celsius
PRESSURE_SCALE = 10  # tenths of a bar

# The deco ceiling is a depth, and it is deliberately **not** given a scale of its own:
# it is drawn against the depth axis, so a ceiling of 3 m and a depth of 3 m have to be
# the same number of centimeters or the shaded region would not line up with the curve it
# bounds. Named all the same, so a reader of the payload doesn't have to know that.
CEILING_SCALE = DEPTH_SCALE

# The six channels a computer's own decompression arithmetic produces, each in the scale
# the format fixes (spec §5.1, §6.4). A scale of 1 is still declared rather than left
# implicit: `channel / SCALE` is what every consumer writes, and a channel with no constant
# beside its siblings is the one a reader assumes must be scaled like them.
NDL_SCALE = 1  # seconds
TTS_SCALE = 1  # seconds
# Hundredths of a bar rather than tenths, which cannot tell 1.30 from 1.32 - and Shearwater
# exports a computed ppO2 to two decimals. A third pressure scale in this module, and the
# only one of the three that is not a tank pressure.
PPO2_SCALE = 100
# Tenths of a percent, because the two Suunto exports of one dive disagree about the
# resolution: the JSON records `0.069` where the XML rounds to `7`, and the finer reading
# is the one worth keeping. Not the same quantity as a recording's `cns_start`/`cns_end`,
# which are the device's own dive-level figures in whole percent and are neither derived from
# this channel nor a source for it.
CNS_SCALE = 10
# Whole percent, and **the unit rather than a range**: a gradient factor is uncapped above.
# A Suunto Ocean's `gf99` reaches 12 575 on a decompression ascent while the surface
# gradient factor beside it declines smoothly through the same stops, Suunto publishes no
# definition of the field, and nothing in the file accounts for the size - so the number is
# stored as the device wrote it. Clamping it would be a guess wearing a plausible number.
GRADIENT_FACTOR_SCALE = 1
SURFACE_GRADIENT_FACTOR_SCALE = GRADIENT_FACTOR_SCALE

# Every channel that is a single series, in the order §6.4 lists its members - which is
# also the order `DiveProfileRead` declares them and therefore the order they are written
# in an exported document. `pressures` is a list rather than a series and sits between
# `temperature` and `ndl`; it is absent here because nothing that walks this tuple can
# treat it like the rest.
#
# One tuple because a dozen functions in `services/dive_profiles.py` have to touch every
# channel, and spelling nine names out a dozen times is how the tenth comes to be missing
# from one of them. The order is load-bearing for the export: a writer emitting channels in
# whatever order it happened to build them produces a profile that reads down nothing.
SINGLE_SERIES_CHANNELS: tuple[str, ...] = (
    "depth",
    "ceiling",
    "temperature",
    "ndl",
    "tts",
    "ppo2",
    "cns",
    "gradient_factor",
    "surface_gradient_factor",
)

# The same order with `pressure` restored to its §6.4 place, which is what `channels` on the
# read schema is sorted by. A separate tuple rather than a computed insertion, so the order a
# client stacks its curves in is one readable list rather than an index arithmetic nobody
# checks. `pressure` is singular here because that is what the *list* is called in
# `channels` - the wire member beside it is `pressures`.
PROFILE_CHANNEL_ORDER: tuple[str, ...] = (
    "depth",
    "ceiling",
    "temperature",
    "pressure",
    "ndl",
    "tts",
    "ppo2",
    "cns",
    "gradient_factor",
    "surface_gradient_factor",
)


class ProfileEventType(StrEnum):
    """What a marker on the profile chart says happened.

    A closed vocabulary, like `GasRole`, and for the same reason: every format spells the
    same occurrence its own way, and a chart that has to render a marker needs to know what
    it is drawing. Anything a device records that this vocabulary has no value for becomes
    `OTHER` **carrying the device's own wording in `label`** rather than being forced into
    a neighbouring type - `recording_shape.shape_events` drops an `OTHER` with no label,
    which is what stops one from being an unlabelled tick that says nothing.

    **Thirteen of these fourteen values are the format's, and `OTHER` is not one of them.**
    DiveJSON §6.6 makes `type` OPTIONAL and spells "unclassified" as an *absent* type beside
    a required `label`; this enum keeps `OTHER` as the internal spelling of that same fact,
    because a column and a JSONB key both want a value rather than a hole. The boundary is
    where the two meet: the export writes no `type` for an `OTHER`, and the import reads an
    absent or unrecognized `type` back as one. Nothing else in the app needs to know.
    """

    GAS_SWITCH = "gas_switch"
    DEEP_STOP = "deep_stop"
    SAFETY_STOP = "safety_stop"
    BOOKMARK = "bookmark"
    # The alarm classes, seeded from the wording real computers use (§6.6). One value per
    # distinct meaning rather than one per vendor string - "Safety Stop Broken" and
    # "Mandatory Safety Stop Broken" are one occurrence with two spellings, and the spelling
    # travels in `label`.
    ASCENT_RATE = "ascent_rate"
    SAFETY_STOP_MANDATORY = "safety_stop_mandatory"
    SAFETY_STOP_VIOLATION = "safety_stop_violation"
    DEEP_STOP_VIOLATION = "deep_stop_violation"
    CEILING_VIOLATION = "ceiling_violation"
    NDL_REACHED = "ndl_reached"
    PPO2_HIGH = "ppo2_high"
    PRESSURE_LOW = "pressure_low"
    DEPTH_ALARM = "depth_alarm"
    OTHER = "other"


class ProfileProvenance(StrEnum):
    """Where a stored profile's samples came from - a closed three-way question.

    Not `dive_profile.parser_key` itself, which is the storage spelling of the same fact and
    a deliberately overloaded column: it holds the format id the reader answered when the
    samples were read off the recording's files, and one of two sentinels otherwise (see
    `UNREPRODUCIBLE_PROVENANCES` in `services/dive_profiles.py`). That overload is right for
    a column that also has to answer "can this be extracted again"; it is wrong for a wire
    member, because publishing it would make a client hard-code the two sentinel strings and
    treat *every other value* - the open set of format ids, which grows with the reader - as
    the third case.
    The question is closed, so the vocabulary is, like `ProfileEventType` and `GasRole`.

    `FILE` is not "this recording has files": a merged recording keeps whatever files either
    half had (`POST /dives/merge`), and its samples are still `MERGE`. This says what
    produced the samples, which is why it lives on the profile rather than on the recording.
    """

    FILE = "file"
    DIVEJSON_IMPORT = "divejson_import"
    MERGE = "merge"


class GasAttribution(BaseModel):
    """How long one cylinder was breathed, and how deep - the answer a multi-tank dive
    needs and that nothing else in the log records.

    **Stored, not served.** This is the shape of `dive_profile.gas_attribution`, derived
    once at extraction and read back by `services/dive_gas.py`; no response body carries
    it. A Pydantic model rather than a plain dataclass precisely because it is read back
    out of JSONB, where a row written by an older extractor is a real possibility - the
    validation is the seam that turns a stale payload into an error at the read rather
    than an `AttributeError` three frames later.

    One entry per gas number, not per stretch on it: a diver who goes back to their back
    gas after a deco stop has two intervals on it and one cylinder, and it is the cylinder
    the pressures belong to. `seconds` is therefore the total time on that gas and
    `mean_depth_cm` the mean over all of it.

    No pressures here, deliberately - see `compute_multi_tank_gas_use`. What this carries
    is exactly what the profile knows and the mixtures don't.
    """

    gas_number: int
    seconds: int
    # In the same centimeters as the depth channel, and for the same reason: it is a depth,
    # and it is a mean of depth samples.
    mean_depth_cm: int


class DepthSilhouette(BaseModel):
    """A recording's depth curve at a dive card's resolution, on each `GET /dives` row.

    Also the shape of `dive_profile.depth_silhouette`, validated on the way back out of JSONB as
    `GasAttribution` is. Evenly spaced rather than timed, so the values and the span are the
    whole of it.
    """

    span: Annotated[
        int,
        Field(gt=0, description="Milliseconds from the recording's first depth reading to its last"),
    ]
    values: Annotated[
        list[int],
        Field(
            description=f"Integer centimeters (scale {DEPTH_SCALE}): value `i` is the deepest reading in the `i`th of "
            "`len(values)` equal slices of `span`, so the deepest value is the recording's maximum depth"
        ),
    ]


class DiveProfileSeries(BaseModel):
    """One channel as `GET /dive/{uuid}/recording/{rid}/profile` returns it: integer milliseconds and values.

    `times`/`values` rather than the `t`/`v` this served until DiveJSON 1.0: these are the
    format's member names (spec §6.5), and the export embeds this very schema, so the two
    surfaces speak one profile vocabulary. The *stored* JSONB payload still uses `t`/`v` -
    it is not on the wire, and renaming its keys would be a data migration over every
    profile row for no reader's benefit (`to_data`/`to_read_schema` in
    `services/dive_profiles.py` are the translation).
    """

    times: Annotated[
        list[int],
        Field(description="Elapsed milliseconds from the recording's `started_at`, strictly increasing"),
    ]
    values: Annotated[
        list[int], Field(description="Readings in this channel's integer scale - see the channel's field")
    ]


class DiveProfilePressureSeries(DiveProfileSeries):
    gas_number: Annotated[int, Field(description="The cylinder's own gas number, as the device labelled it")]


class DiveProfileEvent(BaseModel):
    """One marker on the profile chart, at an integer millisecond like every series' `times`.

    **`type` is nullable here and `OTHER` never reaches the wire**, which is the one place
    the stored vocabulary and the published one differ. DiveJSON §6.6 spells "the device
    recorded something and nothing in the vocabulary says what" as an *absent* `type` beside
    a `label` that is then REQUIRED; storage spells the same fact `OTHER`, because a JSONB
    key and an enum both want a value rather than a hole. `to_read_schema` is the boundary
    and it maps one onto the other, so this class is the format's shape exactly - which it
    has to be, since `ExportRecording.profile` is this very schema and the document's
    `profile` object is `additionalProperties: false`.

    A client therefore sees `type: null` with a `label` where it used to see
    `type: "other"`, and the two say the same thing. The export drops the null entirely
    (`exclude_none`), which is what §5.4 requires of a writer.
    """

    time: Annotated[int, Field(description="Elapsed milliseconds from the recording's `started_at`")]
    type: Annotated[
        ProfileEventType | None,
        Field(
            default=None,
            description="What happened. **Null means unclassified** - the device recorded something here and this "
            "vocabulary has no word for it - and `label` then carries the device's own wording.",
            examples=[ProfileEventType.GAS_SWITCH],
        ),
    ]
    gas_number: Annotated[
        int | None,
        Field(
            default=None,
            description="For a `gas_switch`, the cylinder switched to - the same label `DiveMixture.gas_number` and "
            "the profile's pressure channels carry. Null when the file recorded a switch without saying to what.",
        ),
    ]
    label: Annotated[
        str | None,
        Field(
            default=None,
            examples=["Ceiling Broken"],
            description="The device's own wording for this event. Always set where `type` is null, which is what "
            "makes an unclassified marker worth rendering; set beside a type wherever the device had wording of its "
            "own, and absent on the types that speak for themselves.",
        ),
    ]


class DiveProfileRead(BaseModel):
    """A dive's full profile, and exactly the DiveJSON `profile` object (spec §§6.4-6.6).

    Values stay in their stored integer scales rather than being divided into meters and
    degrees here: the client is going to map every point through a scale function anyway
    while drawing, so it divides once per point there instead of the API inflating the
    payload with `12.34`-shaped floats. The scales are fixed by the format (see the
    module constants) and mirrored by the frontend's `PROFILE_CHANNELS`.

    **Its member list is the format's, and the format closes it.** `ExportRecording.profile`
    embeds this class, and the schema's `profile` object is `additionalProperties: false` -
    so a member DiveJSON has no slot for cannot be added here without making every exported
    document invalid. That is why the provenance lives on `RecordingProfileRead` below
    rather than on this class: an application fact goes on the application's own shape.
    """

    duration: Annotated[
        int,
        Field(
            description="Elapsed milliseconds covered by the longest sample channel. An event may sit past it - a "
            "marker pressed at the surface after the recorder's last sample is real, and neither it nor this number "
            "is moved to make them agree (DiveJSON spec §6.4)."
        ),
    ]
    depth: Annotated[
        DiveProfileSeries | None, Field(default=None, description=f"Depth in centimeters (scale {DEPTH_SCALE})")
    ]
    ceiling: Annotated[
        DiveProfileSeries | None,
        Field(
            default=None,
            description=f"Deco ceiling in centimeters (scale {CEILING_SCALE}, the same as depth - it is drawn against "
            "the depth axis). Present only while the dive had a ceiling: a gap in `times` is a stretch with no "
            "decompression obligation, not a dropout.",
        ),
    ]
    temperature: Annotated[
        DiveProfileSeries | None,
        Field(default=None, description=f"Water temperature in tenths of a degree Celsius (scale {TEMPERATURE_SCALE})"),
    ]
    pressures: Annotated[
        list[DiveProfilePressureSeries],
        Field(
            default_factory=list,
            description=f"Tank pressure per cylinder, in tenths of a bar (scale {PRESSURE_SCALE})",
        ),
    ]
    # **Declared in §6.4's order, not in the order anything happens to build them**, which
    # is why `pressures` sits above rather than at the end: this class is what the exported
    # document's `profile` object is serialized from, and a profile whose members arrive in
    # build order reads down nothing. The order here is the format's.
    #
    # Each is the device's *own* arithmetic and nothing else can produce it - it depends on
    # the model the device ran, its settings and the diver's exposure history, none of which
    # a logged dive carries. Nothing in this app derives one from depth and a gas fraction.
    ndl: Annotated[
        DiveProfileSeries | None,
        Field(
            default=None,
            description=f"Remaining no-decompression time in seconds (scale {NDL_SCALE}). A zero is a reading - the "
            "moment the dive stopped being a no-decompression dive - and a value at the device's display maximum is "
            "a reading too.",
        ),
    ]
    tts: Annotated[
        DiveProfileSeries | None,
        Field(
            default=None,
            description=f"Time to surface in seconds (scale {TTS_SCALE}), stops included, as the device computed it",
        ),
    ]
    ppo2: Annotated[
        DiveProfileSeries | None,
        Field(
            default=None,
            description=f"The partial pressure of oxygen the device computed, in hundredths of a bar "
            f"(scale {PPO2_SCALE}). What it calculated from the gas it believed it was breathing, not a cell reading.",
        ),
    ]
    cns: Annotated[
        DiveProfileSeries | None,
        Field(
            default=None,
            description=f"The CNS oxygen clock during the dive, in tenths of a percent (scale {CNS_SCALE}). "
            "Unbounded above - real computers report past 100 %.",
        ),
    ]
    gradient_factor: Annotated[
        DiveProfileSeries | None,
        Field(
            default=None,
            description=f"The leading tissue's gradient factor, in whole percent (scale {GRADIENT_FACTOR_SCALE}) - a "
            "device's GF99. Unbounded above: a value over 100 is a compartment past its M-value, and real exports "
            "carry far larger ones.",
        ),
    ]
    surface_gradient_factor: Annotated[
        DiveProfileSeries | None,
        Field(
            default=None,
            description="The gradient factor the leading tissue would have on surfacing directly from here, in whole "
            f"percent (scale {SURFACE_GRADIENT_FACTOR_SCALE}). Unbounded above for the same reason.",
        ),
    ]
    events: Annotated[
        list[DiveProfileEvent],
        Field(default_factory=list, description="Gas switches, stops and device alerts, in time order"),
    ]


class RecordingProfileRead(DiveProfileRead):
    """What `GET /dive/{uuid}/recording/{rid}/profile` serves: the format's profile object
    plus the one thing about it that is this application's fact rather than the format's.

    A subclass rather than a member on `DiveProfileRead`, and rather than a second set of
    profile models: every member name is still declared once, so a future channel or a
    renamed series reaches both surfaces from one edit - which is the whole point of
    *"The profile speaks one vocabulary, storage included"* in `DECISIONS.md`. What the
    subclass buys is that the exported document keeps exactly the members DiveJSON defines,
    on an object the schema closes.
    """

    provenance: Annotated[
        ProfileProvenance,
        Field(
            description="Where these samples came from: `file` (read from this recording's stored files, and "
            "read from them again whenever this app's reader moves on), `divejson_import` (supplied by an "
            "imported document) or `merge` (two recordings' samples folded onto one axis)."
        ),
    ]


class DiveProfileInfo(BaseModel):
    """The dive detail response's answer to "does this dive have a profile, and which
    curves would a chart draw" - without decoding the series themselves.

    The extremes are here in *display* units (meters, degrees Celsius, bar), unlike the
    arrays: these are scalars a human reads, not thousands of points something maps.
    """

    uuid: uuid_pkg.UUID
    duration: Annotated[int, Field(description="Elapsed milliseconds the profile covers - its own `duration`")]
    depth_sample_count: int
    # Always present: `dive_profile.parser_key` is NOT NULL, so every stored profile is one
    # of the three. It is here because a file-less recording is first-class rather than
    # degenerate, and the two ways of being file-less are different things a client has to
    # say differently - samples an import took from a document against samples a merge put
    # together from two recordings. Nothing else in the dive read distinguishes them:
    # `files` is empty for both.
    provenance: Annotated[
        ProfileProvenance,
        Field(description="Where these samples came from - see `RecordingProfileRead.provenance`"),
    ]
    channels: Annotated[
        list[str],
        Field(
            description="Which curves this profile carries, in the order a chart stacks them: any of `depth`, "
            "`ceiling`, `temperature`, `pressure`, `ndl`, `tts`, `ppo2`, `cns`, `gradient_factor`, "
            "`surface_gradient_factor`",
            examples=[["depth", "ceiling", "temperature", "pressure", "ndl", "gradient_factor"]],
        ),
    ]
    # Events have no extremes to derive presence from the way a channel does, so this is
    # the count rather than an entry in `channels` - and it is a count because "3
    # markers" is worth showing next to the chart's toggle where a bare boolean isn't.
    # Null on a profile extracted before events were recorded at all, which is what a
    # backfill run clears.
    event_count: Annotated[int | None, Field(default=None, description="How many event markers this profile carries")]
    max_depth: Annotated[float | None, Field(default=None, description="Deepest recorded sample, in meters")]
    max_ceiling: Annotated[
        float | None,
        Field(default=None, description="Deepest deco ceiling this dive was held to, in meters; null when it had none"),
    ]
    min_temperature: Annotated[float | None, Field(default=None, description="Coldest recorded sample, in Celsius")]
    max_temperature: Annotated[float | None, Field(default=None, description="Warmest recorded sample, in Celsius")]
    min_pressure: Annotated[float | None, Field(default=None, description="Lowest recorded tank pressure, in bar")]
    max_pressure: Annotated[float | None, Field(default=None, description="Highest recorded tank pressure, in bar")]
    # Half of the `v` cache-buster the client sends to `GET /dive/{uuid}/recording/{rid}/profile`,
    # beside `uuid`, so a re-extraction gets its own cache entry rather than being masked for
    # five minutes by the previous one.
    updated_at: datetime | None = None
