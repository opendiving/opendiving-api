"""Parser for ANT/Garmin FIT activity files - Garmin Descent and Suunto's native export.

FIT is a binary, self-describing format shared by both vendors, so one parser covers
them: the device writes a `session` summary, a stream of `record` samples, and
`dive_gas` entries, all with units fixed by the global FIT profile rather than by the
vendor. That is the opposite of the situation the two Suunto parsers are in, where
each export generation invents its own units - here `fitdecode` applies the profile's
scale factors and hands back meters, Celsius, bar and percent directly.

What each vendor adds on top is where they diverge, and both are handled: Garmin writes
`dive_summary` (depth/bottom-time summary) and `tank_update`/`tank_summary` (transmitter
telemetry), while Suunto writes neither and instead attaches developer fields - which is
the format's one real trap, see `_native_value`.
"""

import io
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import fitdecode
from fitdecode.types import DevField

from ...schemas.dive_profile import ParsedPressureSeries, ParsedProfileSchema, ParsedSeries
from ...schemas.parsed_dive import DiveMixtureSchema, ParsedDiveSchema
from .base import DiveParser
from .exceptions import DiveParseError

# Every FIT file carries the ASCII string `.FIT` at offset 8, immediately after the
# 8-byte header preamble. It is the format's only magic number, and unlike the `.fit`
# extension it is not something a rename can fake.
_FIT_MAGIC = b".FIT"
_FIT_MAGIC_OFFSET = 8
_FIT_MAGIC_END = _FIT_MAGIC_OFFSET + len(_FIT_MAGIC)

# The integer scales `parse_profile` emits in - depth in centimeters, temperature in
# tenths of a degree, pressure in tenths of a bar. See `schemas/dive_profile.py`.
_CENTIMETERS_PER_METER = Decimal("100")
_TENTHS_PER_UNIT = Decimal("10")

# `dive_gas.status` values that mean the diver did not breathe this cylinder. A FIT
# device stores its whole configured gas list, so a recreational air dive on a computer
# with two deco gases programmed in would otherwise import three mixtures.
_UNUSED_GAS_STATUS = "disabled"

# Real UTC offsets run from -12:00 to +14:00. A wider gap between `activity.timestamp`
# and `activity.local_timestamp` means one of the two is corrupt, and is treated as
# "no offset recorded" rather than propagated - `timezone()` itself raises beyond
# +/-24 h, which would surface as an unhandled `ValueError` from a bad upload.
_MAX_UTC_OFFSET_MINUTES = 14 * 60


def _native_value(frame: fitdecode.FitDataMessage, name: str) -> Any | None:
    """Read a field by name, ignoring any *developer* field that shares the name.

    This is the one thing a FIT reader has to get right for these files. Suunto's export
    declares developer fields whose names collide with native profile fields, so a
    Suunto `session` carries **two** `max_depth` values: the native one (`uint32`, scale
    1000, exact) and a developer duplicate (`float32`, so 32.41 arrives as
    32.40999984741211). Collecting fields into a dict by name - the obvious way to walk
    `frame.fields`, and what the original `export/parse-fit.py` prototype did - keeps
    whichever came last, which is the lossy one.

    `fitdecode`'s own `get_value()` happens to return the native field here, but only
    because it takes the first match by position and FIT encodes native fields ahead of
    developer ones. Filtering on the type is what actually expresses the intent, so a
    file that orders them differently can't quietly reintroduce float32 noise.
    """
    for field_data in frame.fields:
        if field_data.is_named(name) and not isinstance(field_data.field, DevField):
            return field_data.value
    return None


def _scaled_int(value: float, factor: Decimal) -> int:
    """Scale a reading into the integer units the profile is stored in.

    Via `Decimal(str(value))` rather than `round(value * factor)`, consistent with the
    Suunto parsers: `fitdecode` produces these by dividing a raw integer by the profile's
    scale factor in binary floating point, so a depth of 25.85 m can arrive fractionally
    below it and `round(25.85 * 10)` lands on 258. `str()` recovers the shortest decimal
    that round-trips - the digits the device meant - and `ROUND_HALF_UP` keeps a half a
    half, rather than Python's banker's rounding.

    Takes a plain `float`, unlike its counterparts in the Suunto parsers: every caller
    here has already dropped the samples that had no reading, and an optional parameter
    would need an `or 0` at each call site that would quietly turn "no reading" into a
    reading of zero - which for depth is a real, distinct value (a Suunto Ocean records
    0.0 m at the surface).
    """
    return int((Decimal(str(value)) * factor).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _first_not_none(*values: float | None) -> float | None:
    """The first value that was actually recorded.

    Not `a or b`: these are physical readings, and a legitimately zero one must not fall
    through to the next candidate.
    """
    return next((value for value in values if value is not None), None)


def _series(points: list[tuple[float, int]]) -> ParsedSeries | None:
    """Turn `(seconds, value)` pairs into a time-sorted series, or `None` if there are none.

    A stable sort keyed on the timestamp alone, so two readings that landed on the same
    second keep the order the file listed them in.
    """
    if not points:
        return None
    ordered = sorted(points, key=lambda point: point[0])
    return ParsedSeries(t=[t for t, _ in ordered], v=[v for _, v in ordered])


def _local_offset(activity: fitdecode.FitDataMessage | None) -> timezone | None:
    """Recover the UTC offset the dive was logged in, from the `activity` message.

    Every timestamp in a FIT file is UTC; `activity.local_timestamp` is that same instant
    written as local wall-clock time, so the difference between the two *is* the offset at
    the dive site. Reconstructing it matters because the API rejects a naive `start_time`
    and the frontend keeps whatever offset a parse supplies (`normalizeParsedStartTime`):
    handing back plain UTC would be accepted and would silently log an 11:16 Red Sea dive
    as 09:16.

    `None` when the file has no `activity` message, or when the two timestamps disagree by
    more than any real timezone - the caller then falls back to UTC.
    """
    if activity is None:
        return None
    utc_time = _native_value(activity, "timestamp")
    local_time = _native_value(activity, "local_timestamp")
    if not isinstance(utc_time, datetime) or not isinstance(local_time, datetime):
        return None

    # Rounded to whole minutes: no real zone has sub-minute resolution, and the raw
    # difference is two second-resolution timestamps that may be a second apart.
    offset_minutes = round((local_time - utc_time).total_seconds() / 60)
    if abs(offset_minutes) > _MAX_UTC_OFFSET_MINUTES:
        return None
    return timezone(timedelta(minutes=offset_minutes))


@dataclass(frozen=True, slots=True)
class _TankPressures:
    """What one cylinder started and finished the dive on, in bar.

    Its own type rather than the `tank_summary` frame it may have come from, because it
    just as often comes from the ends of the `tank_update` telemetry instead - see
    `FitParser._tank_pressures`.
    """

    start: float | None
    end: float | None


@dataclass(slots=True)
class _FitScan:
    """Everything one pass over a FIT file collects.

    A FIT file is a stream whose summary messages come *after* the samples they
    summarize, so there is no cheap way to read only the header - the whole file is
    decoded either way. Both entry points therefore do a single pass and take what they
    need from it, rather than seeking.

    Summary messages are kept as frames (there are at most a handful); `record` messages
    are reduced to scalars and per-channel points as they stream past, since a long dive
    is several thousand of them.
    """

    session: fitdecode.FitDataMessage | None = None
    activity: fitdecode.FitDataMessage | None = None
    dive_summary: fitdecode.FitDataMessage | None = None
    gases: list[fitdecode.FitDataMessage] = field(default_factory=list)
    tank_summaries: list[fitdecode.FitDataMessage] = field(default_factory=list)

    depth: list[tuple[datetime, float]] = field(default_factory=list)
    temperature: list[tuple[datetime, int]] = field(default_factory=list)
    # Keyed by the transmitter's ANT id, insertion-ordered so cylinders come out in the
    # order the device first reported them.
    pressure: dict[int, list[tuple[datetime, float]]] = field(default_factory=dict)


class FitParser(DiveParser):
    """Parses ANT/Garmin FIT dive activity files (Garmin Descent, Suunto native export).

    Extracts the fields with a direct equivalent on the `Dive`/`DiveMixture` backend
    models (`models/dive.py`, `models/dive_mixture.py`), plus - separately, via
    `parse_profile` - the per-sample depth/temperature/tank-pressure curves stored as
    `DiveProfile`. FIT activity files carry a great deal more (GPS track, ascent rates,
    CNS/OTU loading, deco ceilings, heart rate, battery telemetry) with nowhere to
    persist it, so none of that is parsed.
    """

    key = "fit"
    # The ANT+ registered type for the format. Same reasoning as the other parsers: it
    # comes from the parser that succeeded, never from the uploader's claimed
    # `Content-Type`, so a stored export is always served back as one of a closed set.
    content_type = "application/vnd.ant.fit"

    @classmethod
    def can_parse(cls, filename: str, content: bytes) -> bool:
        """Whether this is a FIT file: a `.fit` name whose header carries the `.FIT` magic.

        Both checks, and neither alone. The extension keeps this in line with the other
        parsers and costs nothing; the magic is what actually decides, since `.fit` is
        not a format guarantee. Purely syntactic - no decoding happens here, so an
        unreadable file is rejected by `parse()` with a reason rather than silently
        skipped as "not FIT".
        """
        if not filename.lower().endswith(".fit"):
            return False
        return content[_FIT_MAGIC_OFFSET:_FIT_MAGIC_END] == _FIT_MAGIC

    @classmethod
    def parse(cls, content: bytes) -> ParsedDiveSchema:
        """Extract the dive itself (not its samples - see `parse_profile`)."""
        scan = cls._scan(content)
        try:
            return cls._parse_dive(scan)
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise DiveParseError(f"Malformed FIT dive data: {exc}") from exc

    @classmethod
    def parse_profile(cls, content: bytes) -> ParsedProfileSchema | None:
        """Extract the `record` stream (and Garmin's `tank_update`s) as per-channel series."""
        scan = cls._scan(content)
        try:
            return cls._parse_samples(scan)
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise DiveParseError(f"Malformed FIT dive samples: {exc}") from exc

    @classmethod
    def _scan(cls, content: bytes) -> _FitScan:
        """Decode the file once, keeping the messages a dive is built from.

        `fitdecode`'s defaults are deliberately kept: `CrcCheck.WARN` and
        `ErrorHandling.WARN` mean a file whose checksum or trailing bytes are slightly
        off still imports. The CRC here guards against transfer corruption, not
        tampering - nothing downstream trusts it - and refusing an otherwise readable
        dive log over it would lose real data for no gain.
        """
        scan = _FitScan()
        try:
            with fitdecode.FitReader(io.BytesIO(content)) as fit:
                for frame in fit:
                    if isinstance(frame, fitdecode.FitDataMessage):
                        cls._collect(scan, frame)
        except fitdecode.FitError as exc:
            raise DiveParseError(f"Invalid FIT file: {exc}") from exc
        return scan

    @classmethod
    def _collect(cls, scan: _FitScan, frame: fitdecode.FitDataMessage) -> None:
        """Route one decoded message into the scan.

        Only the *first* `session`/`activity`/`dive_summary` is kept. A dive export
        normally holds exactly one of each; where a device writes more (a multi-sport
        file, a repetitive-dive series), the first is the one this file is about, and
        `ParsedDiveSchema` describes a single dive either way.
        """
        if frame.name == "session":
            if scan.session is None:
                scan.session = frame
        elif frame.name == "activity":
            if scan.activity is None:
                scan.activity = frame
        elif frame.name == "dive_summary":
            if scan.dive_summary is None:
                scan.dive_summary = frame
        elif frame.name == "dive_gas":
            scan.gases.append(frame)
        elif frame.name == "tank_summary":
            scan.tank_summaries.append(frame)
        elif frame.name == "record":
            cls._collect_record(scan, frame)
        elif frame.name == "tank_update":
            cls._collect_tank_update(scan, frame)

    @staticmethod
    def _collect_record(scan: _FitScan, frame: fitdecode.FitDataMessage) -> None:
        """Take depth and temperature off one `record`.

        Channels are sampled independently, so a record may carry either, both or
        neither: a Suunto Ocean dive writes 4 295 records of which only 431 carry
        `depth` while 4 294 carry `temperature`, whereas a D5 writes both on every
        record at 0.1 Hz. Hence a list per channel rather than a shared axis, which on
        the Ocean would be 90 % null in the depth column.
        """
        timestamp = _native_value(frame, "timestamp")
        if not isinstance(timestamp, datetime):
            # No axis, no sample. Nothing can be placed on a channel without one.
            return

        depth = _native_value(frame, "depth")
        if depth is not None:
            scan.depth.append((timestamp, depth))

        temperature = _native_value(frame, "temperature")
        if temperature is not None:
            scan.temperature.append((timestamp, temperature))

    @staticmethod
    def _collect_tank_update(scan: _FitScan, frame: fitdecode.FitDataMessage) -> None:
        """Take one transmitter pressure reading (Garmin Descent with a T-series pod).

        `pressure` is already bar - the FIT profile scales it - unlike the Pascal and
        millibar the two Suunto exports use. Readings are grouped by `sensor`, the pod's
        ANT id, because a Descent Mk2i/Mk3i can pair several.
        """
        timestamp = _native_value(frame, "timestamp")
        pressure = _native_value(frame, "pressure")
        sensor = _native_value(frame, "sensor")
        if not isinstance(timestamp, datetime) or pressure is None or sensor is None:
            return
        scan.pressure.setdefault(int(sensor), []).append((timestamp, pressure))

    @classmethod
    def _parse_dive(cls, scan: _FitScan) -> ParsedDiveSchema:
        """Map the scanned messages onto `ParsedDiveSchema`."""
        session = cls._dive_session(scan)
        summary = scan.dive_summary

        start_time = _native_value(session, "start_time")
        offset = _local_offset(scan.activity)
        if isinstance(start_time, datetime) and offset is not None:
            start_time = start_time.astimezone(offset)

        # `total_elapsed_time` first: it is the wall clock from the moment the dive
        # started to the moment it ended, which is what a diver means by duration and
        # what `session.timestamp - session.start_time` agrees with. `total_timer_time`
        # (which excludes pauses, a distinction that barely exists underwater) stands in
        # where a device omits it. Garmin's `dive_summary.bottom_time` is deliberately
        # last and only a fallback: it measures time *at depth*, not the whole dive.
        duration = _first_not_none(
            _native_value(session, "total_elapsed_time"),
            _native_value(session, "total_timer_time"),
            _native_value(summary, "bottom_time") if summary is not None else None,
        )

        return ParsedDiveSchema(
            avg_depth=cls._depth(session, summary, "avg_depth"),
            bottom_temperature=cls._bottom_temperature(scan, session),
            # Deliberately not parsed, though `session.dive_number` is right there. It is
            # the *computer's* counter, not the diver's lifetime dive number - it starts
            # at 1 on a new or factory-reset device. The example corpus shows this
            # outright: a D5 export whose `dive_number` is 5 carries the diver's own
            # label for the same dive in `session.description`, "#28: Elphinstone Reef".
            # Importing it would stamp a dive #5 onto someone's 28th dive. Both Suunto
            # parsers leave this null for the same reason; the number comes from
            # `GET /dives/next-number` instead - see `services/dive_numbering.py`.
            dive_number=None,
            duration=round(duration) if duration is not None else None,
            max_depth=cls._depth(session, summary, "max_depth"),
            start_time=start_time.isoformat() if isinstance(start_time, datetime) else None,
            mixtures=cls._mixtures(scan),
        )

    @staticmethod
    def _dive_session(scan: _FitScan) -> fitdecode.FitDataMessage:
        """The `session` this file's dive is described by.

        `sport == "diving"` covers every dive sub-sport (single/multi-gas, gauge, apnea)
        and is what both vendors write. A session that doesn't say so is still accepted
        when the file carried depth samples, since that is the stronger evidence and
        costs nothing to check.

        A FIT file with no dive in it - a bike ride, a run - is a `DiveParseError` rather
        than an `UnsupportedDiveFileError`, which is a deliberate departure from the
        Suunto parsers. `UnsupportedDiveFileError` means "not my format, let the next
        parser try", and would surface to the diver as a 415 "no parser available for
        this file" - which is untrue and unhelpful here. The file *is* a FIT file this
        parser read successfully; it simply holds no dive, and saying exactly that in a
        422 is the more useful answer.
        """
        if scan.session is None:
            raise DiveParseError("This FIT file contains no session, so there is no dive to import.")

        sport = _native_value(scan.session, "sport")
        if sport != "diving" and not scan.depth:
            raise DiveParseError(f"This FIT file records a {sport or 'non-diving'} activity, not a dive.")
        return scan.session

    @staticmethod
    def _depth(session: fitdecode.FitDataMessage, summary: fitdecode.FitDataMessage | None, name: str) -> float | None:
        """A depth off the `session`, falling back to Garmin's `dive_summary`.

        The two agree when both are present; the fallback is for devices that summarize
        a dive in one message and not the other.
        """
        depth = _native_value(session, name)
        if depth is None and summary is not None:
            depth = _native_value(summary, name)
        return depth

    @staticmethod
    def _bottom_temperature(scan: _FitScan, session: fitdecode.FitDataMessage) -> float | None:
        """The coldest water this dive saw - `session.min_temperature`, else the coldest sample.

        `max_temperature` is pointedly *not* consulted, despite being the field Suunto
        actually populates: on both Ocean exports in the corpus it holds 22 °C while the
        samples run 22-25 °C, i.e. Suunto writes the *coldest* reading into a field named
        for the warmest. Reading it as a maximum would be wrong, and reading it as a
        minimum would bake one vendor's bug into the parser. The sample stream is ground
        truth and gives the same 22 °C, so it is used instead.
        """
        recorded = _native_value(session, "min_temperature")
        if recorded is not None:
            return float(recorded)
        return float(min(value for _, value in scan.temperature)) if scan.temperature else None

    @classmethod
    def _mixtures(cls, scan: _FitScan) -> list[DiveMixtureSchema]:
        """Map `dive_gas` entries onto `DiveMixture`s, with Garmin tank pressures where present.

        Gases are keyed by `message_index` (FIT's index for repeated messages), deduped
        on it keeping the first, and emitted in index order - a device may re-announce
        its gas list mid-file.
        """
        gases: dict[int, fitdecode.FitDataMessage] = {}
        for position, gas in enumerate(scan.gases):
            if _native_value(gas, "status") == _UNUSED_GAS_STATUS:
                continue
            index = _native_value(gas, "message_index")
            gases.setdefault(int(index) if index is not None else position, gas)

        ordered = [gases[index] for index in sorted(gases)]
        return [cls._mixture(gas, tank) for gas, tank in zip(ordered, cls._tanks_for(scan, ordered), strict=True)]

    @classmethod
    def _tanks_for(cls, scan: _FitScan, gases: list[fitdecode.FitDataMessage]) -> list[_TankPressures | None]:
        """Line the cylinders that reported pressure up with the gases they belong to.

        There is no field linking the two: tank telemetry is keyed by the transmitter's
        ANT id and a `dive_gas` by its `message_index`, and nothing in the file maps one
        onto the other. Position is the only available signal, so they are paired in
        order - and only when the counts match exactly. Anything else (two gases, one
        pod) leaves every pressure null rather than guessing, because these feed
        `compute_gas_use` and a confidently wrong start pressure produces a plausible,
        wrong RMV, which is worse than an empty field the diver can fill in.
        """
        tanks = cls._tank_pressures(scan)
        return list(tanks) if len(tanks) == len(gases) else [None] * len(gases)

    @staticmethod
    def _tank_pressures(scan: _FitScan) -> list[_TankPressures]:
        """What each cylinder started and finished the dive on.

        Two sources, in order of authority. `tank_summary` is the device's own summary and
        is used when it wrote one. Failing that the figures are taken off the ends of the
        `tank_update` telemetry - the first and last reading a pod sent - because a Descent
        streams `tank_update` throughout the dive whether or not it also emits a summary,
        and deriving two numbers from that stream is better than dropping the transmitter
        data on the floor. Readings are ordered by their own timestamps rather than by
        arrival, since separate pods interleave.
        """
        if scan.tank_summaries:
            return [
                _TankPressures(
                    start=_native_value(summary, "start_pressure"),
                    end=_native_value(summary, "end_pressure"),
                )
                for summary in scan.tank_summaries
            ]

        return [
            _TankPressures(start=readings[0][1], end=readings[-1][1])
            for readings in (sorted(unordered, key=lambda reading: reading[0]) for unordered in scan.pressure.values())
            if readings
        ]

    @staticmethod
    def _mixture(gas: fitdecode.FitDataMessage, tank: _TankPressures | None) -> DiveMixtureSchema:
        """Map one `dive_gas` (plus its cylinder's pressures, if any) onto a `DiveMixture`.

        No unit conversion: the FIT profile already defines `oxygen_content`/
        `helium_content` as whole percent and tank pressures as bar, which is what the
        model stores.
        """
        oxygen = _native_value(gas, "oxygen_content")
        helium = _native_value(gas, "helium_content")
        return DiveMixtureSchema(
            end_pressure=tank.end if tank is not None else None,
            helium=float(helium) if helium is not None else None,
            # Left for the user to fill in themselves rather than parsed - see
            # DECISIONS.md.
            name=None,
            oxygen=float(oxygen) if oxygen is not None else None,
            start_pressure=tank.start if tank is not None else None,
            # FIT has nowhere to record cylinder size at all - not on `dive_gas`, and
            # `tank_summary` carries only the volume *consumed*. `None`, not 0.0: the
            # format cannot express this, so the dive form applies its own
            # `DEFAULT_MIXTURE` rather than being handed a cylinder of no volume.
            volume=None,
        )

    @staticmethod
    def _earliest_sample(scan: _FitScan) -> datetime:
        """The first reading on any channel, for a file whose `session` has no start time.

        Only reached when there is at least one reading, which `_parse_samples` has
        already established.
        """
        timestamps = [timestamp for timestamp, _ in scan.depth]
        timestamps += [timestamp for timestamp, _ in scan.temperature]
        for readings in scan.pressure.values():
            timestamps += [timestamp for timestamp, _ in readings]
        return min(timestamps)

    @classmethod
    def _parse_samples(cls, scan: _FitScan) -> ParsedProfileSchema | None:
        """Turn the scanned sample streams into one series per channel.

        Timestamps are rebased onto the dive's start as fractional seconds. The origin is
        the `session` start where there is one, so every channel shares an axis; absolute
        zero doesn't matter (`services/dive_profiles.py` rebases onto the earliest
        reading across all channels anyway), only that the channels agree on it.
        """
        if not scan.depth and not scan.temperature and not scan.pressure:
            return None

        start_time = _native_value(scan.session, "start_time") if scan.session is not None else None
        origin: datetime = start_time if isinstance(start_time, datetime) else cls._earliest_sample(scan)

        def elapsed(timestamp: datetime) -> float:
            return (timestamp - origin).total_seconds()

        return ParsedProfileSchema(
            depth=_series([(elapsed(t), _scaled_int(v, _CENTIMETERS_PER_METER)) for t, v in scan.depth]),
            temperature=_series([(elapsed(t), _scaled_int(v, _TENTHS_PER_UNIT)) for t, v in scan.temperature]),
            # Labelled 1, 2, ... in the order the device first reported each pod, never by
            # `sensor` - that is an ANT id (a serial, e.g. 2411100050), so it would read as
            # nonsense in a chart legend. Same reasoning as the XML parser's refusal to use
            # `<TransmitterId>`, and it keeps a single-cylinder dive labelled gas 1 there
            # and here alike.
            pressure=[
                ParsedPressureSeries(gas_number=number, t=series.t, v=series.v)
                for number, series in (
                    (position, _series([(elapsed(t), _scaled_int(v, _TENTHS_PER_UNIT)) for t, v in readings]))
                    for position, readings in enumerate(scan.pressure.values(), start=1)
                )
                if series is not None
            ],
        )
