"""Wire and parser shapes for a dive's per-sample profile.

Deliberately its own module rather than an addition to `parsed_dive.py`: that one is the
`POST /dive/parse` contract, and a profile never travels over it. A profile is thousands
of readings the browser has no use for while filling in a form, and it would have to be
posted back to be stored - which would make the stored samples client-supplied and
reopen the exact trust problem the parse token exists to close.

One shape here is neither wire nor parser: `GasAttribution` is what a summary *column*
holds. It lives here rather than in the service because it is read back out of JSONB and
wants validating on the way in, which is what the models in this package are for.
"""

import uuid as uuid_pkg
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, Field, model_validator

# The integer scales every `v` array is expressed in. They live here (and, mirrored, in
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


class ProfileEventType(StrEnum):
    """What a marker on the profile chart says happened.

    A closed vocabulary, like `GasRole`, and for the same reason: three formats spell the
    same occurrence three ways, and a chart that has to render a marker needs to know what
    it is drawing. Anything a device records that isn't one of the first four becomes
    `OTHER` **carrying the device's own wording in `label`** rather than being forced into
    a neighbouring type - see the normalization tables in the parsers, and `_validate_events`
    below, which is what stops an `OTHER` from being an unlabelled tick that says nothing.
    """

    GAS_SWITCH = "gas_switch"
    DEEP_STOP = "deep_stop"
    SAFETY_STOP = "safety_stop"
    BOOKMARK = "bookmark"
    OTHER = "other"


def _validate_series(t: list[float] | list[int], v: list[int], label: str) -> None:
    if len(t) != len(v):
        raise ValueError(f"{label}: series has {len(t)} timestamps but {len(v)} values")
    if not t:
        raise ValueError(f"{label}: series is empty (a channel with no readings must be omitted, not empty)")
    if any(later < earlier for earlier, later in zip(t, t[1:], strict=False)):
        raise ValueError(f"{label}: timestamps are not sorted")


def _validate_events(events: list[ParsedProfileEvent]) -> None:
    """The events' own invariants, which are not the series' invariants.

    Its own function rather than a call to `_validate_series` with the timestamps pulled
    out, because the two have almost nothing in common. An event has no `v` to be the same
    length as, an empty list is the ordinary case rather than a channel that should have
    been omitted, and - the part that would actually have been wrong - events are **not**
    required to arrive sorted. A parser emits them in whatever order the file lists them,
    and the Suunto XML export lists gas changes nested inside each `<DiveMixture>`, so the
    second cylinder's switch at 2 356 s can follow the first's at 0 s or precede it
    depending on how the cylinders were ordered. Sorting is done once in `normalize`,
    where a single stream makes it unambiguous - unlike the sample channels, where only a
    parser knows which timestamps belong to which sensor.

    What is enforced is that an `OTHER` says what it was. The point of the escape hatch is
    to surface a device's own wording; an `OTHER` with no `label` is a tick on a chart that
    tells the diver nothing, and would mean a parser dropped the one thing it had to keep.
    """
    for event in events:
        if event.type is ProfileEventType.OTHER and not (event.label or "").strip():
            raise ValueError(f"event at {event.t}s is `other` with no label (the device's own wording is the point)")


class ParsedSeries(BaseModel):
    """One channel as a parser produces it: raw seconds, already in this channel's scale.

    `t` is seconds from the file's own origin (`Header.DateTime` for the JSON export, the
    `<Time>` axis for the XML one) and may be fractional and non-zero-based - rebasing,
    rounding and deduping are format-independent and happen once in
    `services/dive_profiles.py`. Sorting, however, is the parser's job: only it knows
    which timestamps belong to which sensor stream, and the union of a Suunto Ocean
    export's sample timestamps is *not* monotonic (adjacent entries go backwards by up to
    0.7 s, because separate streams are appended out of order). Each channel's own
    timestamps are monotonic, so a parser that groups by channel before emitting satisfies
    this for free.

    `v` is already integer-scaled by the parser, because the parser is the only layer that
    knows a format's units (mbar here, Pascal there, Kelvin over there) and nothing
    downstream should have to.
    """

    t: list[float]
    v: list[int]


class ParsedPressureSeries(ParsedSeries):
    """A single cylinder's pressure readings, labelled by the gas number it reported as.

    Pressure is a list of these rather than a scalar channel because it is genuinely
    multi-tank: a Suunto Ocean reports five cylinder slots, and gas numbering differs
    between device generations (1 on the 2025 D5, 0 on the Ocean). `gas_number` is a
    label to display, not an index to trust.
    """

    gas_number: int


class ParsedProfileEvent(BaseModel):
    """One thing the device recorded happening, at a moment rather than over a channel.

    `t` is in the same raw seconds-from-the-file's-origin as `ParsedSeries.t`, and is
    rebased by `normalize` against the *sample* channels' origin - never against the
    events' own earliest, which would let one mistimed marker slide every event on the
    chart away from the curve it annotates.

    `gas_number` is set only on a `GAS_SWITCH`, and is the same label the mixtures and the
    profile's pressure channels use, so a switch marker and the cylinder it switched to
    can be joined. `None` where the file records that a switch happened without saying to
    what. `label` carries the device's own wording, and is required on an `OTHER`.
    """

    t: float
    type: ProfileEventType
    gas_number: int | None = None
    label: str | None = None


class ParsedProfileSchema(BaseModel):
    """Everything `DiveParser.parse_profile` returns for one file.

    A channel the file doesn't carry is absent (`None` / empty list), never an empty
    series - "this export has no transmitter" and "this export has a transmitter that
    recorded nothing" are the same thing to a chart, and both mean "don't draw the axis".
    """

    depth: ParsedSeries | None = None
    ceiling: ParsedSeries | None = None
    temperature: ParsedSeries | None = None
    pressure: list[ParsedPressureSeries] = Field(default_factory=list)
    events: list[ParsedProfileEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_series(self) -> Self:
        if self.depth is not None:
            _validate_series(self.depth.t, self.depth.v, "depth")
        if self.ceiling is not None:
            _validate_series(self.ceiling.t, self.ceiling.v, "ceiling")
        if self.temperature is not None:
            _validate_series(self.temperature.t, self.temperature.v, "temperature")
        for cylinder in self.pressure:
            _validate_series(cylinder.t, cylinder.v, f"pressure[gas {cylinder.gas_number}]")
        _validate_events(self.events)
        return self


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


class DiveProfileSeries(BaseModel):
    """One channel as `GET /dive/{uuid}/profile` returns it: integer seconds, integer values."""

    t: Annotated[list[int], Field(description="Elapsed seconds from the start of the dive, strictly increasing")]
    v: Annotated[list[int], Field(description="Readings in this channel's integer scale - see the channel's field")]


class DiveProfilePressureSeries(DiveProfileSeries):
    gas_number: Annotated[int, Field(description="The cylinder's own gas number, as the device labelled it")]


class DiveProfileEvent(BaseModel):
    """One marker on the profile chart, at an integer second like every series' `t`."""

    t: Annotated[int, Field(description="Elapsed seconds from the start of the dive")]
    type: Annotated[ProfileEventType, Field(description="What happened", examples=[ProfileEventType.GAS_SWITCH])]
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
            description="The device's own wording for this event. Always set on an `other`, which is what makes that "
            "type worth rendering; absent on the types that speak for themselves.",
        ),
    ]


class DiveProfileRead(BaseModel):
    """A dive's full profile.

    Values stay in their stored integer scales rather than being divided into meters and
    degrees here: the client is going to map every point through a scale function anyway
    while drawing, so it divides once per point there instead of the API inflating the
    payload with `12.34`-shaped floats. The scales are fixed by the format (see the
    module constants) and mirrored by the frontend's `PROFILE_CHANNELS`.
    """

    duration_seconds: Annotated[int, Field(description="Elapsed seconds covered by the longest channel")]
    depth: Annotated[
        DiveProfileSeries | None, Field(default=None, description=f"Depth in centimeters (scale {DEPTH_SCALE})")
    ]
    ceiling: Annotated[
        DiveProfileSeries | None,
        Field(
            default=None,
            description=f"Deco ceiling in centimeters (scale {CEILING_SCALE}, the same as depth - it is drawn against "
            "the depth axis). Present only while the dive had a ceiling: a gap in `t` is a stretch with no "
            "decompression obligation, not a dropout.",
        ),
    ]
    temperature: Annotated[
        DiveProfileSeries | None,
        Field(default=None, description=f"Water temperature in tenths of a degree Celsius (scale {TEMPERATURE_SCALE})"),
    ]
    pressure: Annotated[
        list[DiveProfilePressureSeries],
        Field(
            default_factory=list,
            description=f"Tank pressure per cylinder, in tenths of a bar (scale {PRESSURE_SCALE})",
        ),
    ]
    events: Annotated[
        list[DiveProfileEvent],
        Field(default_factory=list, description="Gas switches, stops and device alerts, in time order"),
    ]


class DiveProfileInfo(BaseModel):
    """The dive detail response's answer to "does this dive have a profile, and which
    curves would a chart draw" - without decoding the series themselves.

    The extremes are here in *display* units (meters, degrees Celsius, bar), unlike the
    arrays: these are scalars a human reads, not thousands of points something maps.
    """

    uuid: uuid_pkg.UUID
    duration_seconds: int
    depth_sample_count: int
    channels: Annotated[
        list[str],
        Field(
            description="Which curves this profile carries: any of `depth`, `ceiling`, `temperature`, `pressure`",
            examples=[["depth", "ceiling", "temperature", "pressure"]],
        ),
    ]
    # Events have no extremes to derive presence from the way a channel does, so this is
    # the count rather than a sixth entry in `channels` - and it is a count because "3
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
    # The `v` cache-buster the client sends to `GET /dive/{uuid}/profile`, so a
    # re-extraction gets its own cache entry rather than being masked for five minutes by
    # the previous one.
    updated_at: datetime | None = None
