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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import chain
from typing import Any

import fitdecode
from fitdecode.types import DevField, FieldData

from ...schemas.dive_mixture import GasRole
from ...schemas.dive_profile import (
    ParsedPressureSeries,
    ParsedProfileEvent,
    ParsedProfileSchema,
    ProfileEventType,
)
from ...schemas.parsed_dive import DiveMixtureSchema, ParsedDiveSchema
from .base import DiveParser
from .channels import CENTIMETERS_PER_METER, TENTHS_PER_UNIT, ceiling_cm, scaled_int, series
from .exceptions import EXTRACTION_ERRORS, DiveParseError

# Every FIT file carries the ASCII string `.FIT` at offset 8, immediately after the
# 8-byte header preamble. It is the format's only magic number, and unlike the `.fit`
# extension it is not something a rename can fake.
_FIT_MAGIC = b".FIT"
_FIT_MAGIC_OFFSET = 8
_FIT_MAGIC_END = _FIT_MAGIC_OFFSET + len(_FIT_MAGIC)

# `dive_gas.status` values that mean the diver did not breathe this cylinder. A FIT
# device stores its whole configured gas list, so a recreational air dive on a computer
# with two deco gases programmed in would otherwise import three mixtures. The profile's
# enum is `{0: disabled, 1: enabled, 2: backup_only}`, and `backup_only` belongs here for
# exactly the reason the name says: a pony bottle that was carried and not breathed. Left
# in, it costs the dive its SAC/RMV, since `compute_gas_use` needs exactly one mixture.
_UNUSED_GAS_STATUSES = frozenset({"disabled", "backup_only"})

# Real UTC offsets run from -12:00 to +14:00. A wider gap between `activity.timestamp`
# and `activity.local_timestamp` means one of the two is corrupt, and is treated as
# "no offset recorded" rather than propagated - `timezone()` itself raises beyond
# +/-24 h, which would surface as an unhandled `ValueError` from a bad upload.
_MAX_UTC_OFFSET_MINUTES = 14 * 60

# How many frames a file may hold before it stops looking like a dive and starts looking
# like a denial-of-service. Decoding is linear in frames and is *the* cost here: a 5 MB
# file (`MAX_DIVE_FILE_SIZE`) of bare 10-byte `record` messages, which is how a device
# actually encodes a long log, holds ~524 000 of them and takes ~10 s to decode - paid
# twice per import, since `POST /dive/parse` and `PUT /dive/{uuid}/file` each read the
# file. Stopping at this cap holds that to ~1.5 s.
#
# The largest real file in the corpus is a 72-minute multi-channel Suunto Ocean dive at
# 4 339 frames - about one per second - so this is ~23x that, or roughly 28 hours of
# continuous logging. Raise it with evidence if a real dive ever comes close.
#
# Deliberately raises rather than truncating. A FIT file's `session` is written *after*
# the samples it summarizes, so stopping early and keeping what we have would discard
# the start time, duration and depths, and import a confidently empty dive.
#
# This bounds CPU, and memory only as a side effect: a file at the cap grows RSS by
# ~44 MB, since the per-channel sample lists and the retained summary frames all scale
# with frame count, and `run_in_threadpool` will run up to AnyIO's 40 of these at once.
# Left alone deliberately - `MAX_DIVE_FILE_SIZE` and upload concurrency bound it in
# practice, and picking a memory cap needs a deployment's actual ceiling rather than a
# number chosen here.
_MAX_FRAMES = 100_000

# `message_index` is a bitfield, not a plain counter: the low 12 bits are the index and
# the top bits are flags ("selected", plus a reserved span). The profile spells the mask
# out as its own enum entry, `{4095: 'mask'}`. Without it, a gas at index 1 with the
# selected bit set arrives as 32769 and sorts *after* an unflagged gas at index 2, which
# reorders the mixtures and so misaligns `_tanks_for`'s positional pairing.
_MESSAGE_INDEX_MASK = 0x0FFF

# `event.event` values worth a marker on the chart, and what each becomes. A normalization
# table rather than a cast, like the Suunto parsers': this is Garmin's vocabulary, and the
# 30-odd members it does not list (`timer`, `battery_low`, `tank_pod_connected`, every
# cycling and running alert the shared enum carries) must come out as nothing rather than
# be forced into a type of ours.
#
# `timer` is the deliberate omission among them. It is the only `event` any file in the
# corpus writes, and its `start`/`stop` pair is where the dive begins and ends - which the
# profile's own axis already says, twice over.
#
# `dive_alert` is mapped to `OTHER` rather than being decoded further because its `data`
# subfield is a 40-member enum (`deco_ceiling_broken`, `po2_crit_high`, `cns_warning`, ...)
# that the profile spells out in words. Passing those words through as the label is the
# same choice the JSON parser makes with `Alarm`/`Warning`, and for the same reason.
_EVENT_TYPE_BY_NAME = {
    "dive_gas_switched": ProfileEventType.GAS_SWITCH,
    "user_marker": ProfileEventType.BOOKMARK,
    "dive_alert": ProfileEventType.OTHER,
}

# How many cylinders one dive may describe, **in total**. No device pairs more than a
# handful of transmitters - a Descent Mk3i tops out around five - so a file claiming
# hundreds is describing something other than a dive.
#
# Bounded because each cylinder becomes a `DiveMixtureSchema` in the `/dive/parse`
# response and a pressure channel in the stored profile, neither of which `_MAX_FRAMES`
# constrains. Every list that feeds those is truncated to it - the gas list, the tank
# summaries, the telemetry sensors, and the merged result - rather than any one of them,
# since capping the sources separately still let their union run to three times this.
_MAX_CYLINDERS = 16

# How many event markers one dive may describe. Bounded here as well as in
# `services/dive_profiles.py` for the reason `_MAX_CYLINDERS` is: `_MAX_FRAMES` allows
# 100 000 frames, and an `event` is a handful of bytes, so a small file can carry far more
# of them than the extractor's own cap would ever store - and this parser would build every
# one into a `ParsedProfileEvent` first. Deliberately the smaller of the two numbers, so
# hitting it is a property of the file rather than of which cap ran first: the worst dive
# in the corpus produces 17 markers.
_MAX_EVENTS = 100


def _native_value(frame: fitdecode.FitDataMessage, name: str) -> Any | None:
    """Read a field by name, ignoring any *developer* field that shares the name.

    This is the one thing a FIT reader has to get right for these files. Suunto's export
    declares developer fields whose names collide with native profile fields, so a
    Suunto `session` carries **two** `max_depth` values: the native one (`uint32`, scale
    1000, exact) and a developer duplicate (`float32`, so 32.41 arrives as
    32.40999984741211). Collecting fields into a dict by name - the obvious way to walk
    `frame.fields`, and what the original `export/parse-fit.py` prototype did - keeps
    whichever came last, which is the lossy one.

    `fitdecode`'s own `get_value()` returns the native field *when both are present*, but
    only as a side effect of taking the first match by position: a definition record
    carries native field definitions ahead of developer ones, so a valid file cannot order
    them the other way round. Where the two genuinely differ is a message carrying **only**
    the developer duplicate - `get_value` then hands back a vendor's float32 as though it
    were the profile's scaled `uint32`, units and semantics included, while this returns
    `None` and lets the caller fall back to a field that means what it says.
    """
    field_data = _native_field(frame, name)
    return field_data.value if field_data is not None else None


def _native_field(frame: fitdecode.FitDataMessage, name: str) -> FieldData | None:
    for field_data in frame.fields:
        if field_data.is_named(name) and not isinstance(field_data.field, DevField):
            return field_data
    return None


def _native_raw(frame: fitdecode.FitDataMessage, name: str) -> Any | None:
    """A field's *undecoded* value, for the ones that are identities rather than readings.

    `fitdecode` renders a field whose profile type carries an enum by exact value match,
    and FIT keeps bitfield masks in that same enum slot - `message_index` maps
    `{4095: 'mask', 28672: 'reserved', 32768: 'selected'}` and `ant_channel_id`, the type
    behind `tank_update`/`tank_summary`'s `sensor`, maps `{65535: 'ant_device_number',
    ...}`. So a gas index or an ANT id that happens to land on one of those numbers comes
    back as a **string**, and `int()` on it raises.

    That is not a corrupt-file problem. `message_index = 0x8000` is gas index 0 with the
    spec's "selected" bit set, which is an ordinary thing for a device to write about the
    first configured gas - and it made the whole dive un-importable with a 422.
    """
    field_data = _native_field(frame, name)
    return field_data.raw_value if field_data is not None else None


def _role(gas: fitdecode.FitDataMessage | None) -> GasRole | None:
    """What a `dive_gas` says the cylinder was carried for, where it says anything.

    `mode` is the only field on the message that speaks to this, and it answers only one
    half of the question: its enum is `{0: open_circuit, 1: closed_circuit_diluent}`, so
    a diluent identifies itself outright while `open_circuit` covers a back gas and a
    stage bottle alike and therefore maps to nothing.

    Pointedly **not** derived from `status`, the other candidate. That is
    `enabled`/`disabled`/`backup_only` - whether the gas was breathed at all, which
    `_breathed_gases` already uses it for - and reading `backup_only` as a role would
    relabel a pony bottle as though the file had described its purpose.
    """
    if gas is None:
        return None
    return GasRole.DILUENT if _native_value(gas, "mode") == "closed_circuit_diluent" else None


def _first_not_none(*values: float | None) -> float | None:
    """The first value that was actually recorded.

    Not `a or b`: these are physical readings, and a legitimately zero one must not fall
    through to the next candidate.
    """
    return next((value for value in values if value is not None), None)


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


# A cylinder nothing has said anything about yet - the identity for `_merge_pressures`,
# and what a `tank_summary` carrying neither pressure amounts to.
_NO_PRESSURES = _TankPressures(start=None, end=None)


def _merge_pressures(summary: _TankPressures, telemetry: _TankPressures) -> _TankPressures:
    """One cylinder's figures, preferring its summary and filling gaps from its telemetry.

    Per field, not per source: a summary that recorded a start and lost the end to a pod
    dropout still contributes the start, and the telemetry supplies what it couldn't.
    """
    return _TankPressures(
        start=_first_not_none(summary.start, telemetry.start),
        end=_first_not_none(summary.end, telemetry.end),
    )


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
    # How many `session` messages have gone past, which is what bounds
    # `dive_summaries` to the first dive - see `_collect`.
    session_count: int = 0
    # Every `dive_summary` in the file, resolved by `_dive_summary` rather than kept as
    # "the first one": a Garmin freediving activity writes one per individual dive plus a
    # session-level one, and only the latter describes the activity being imported.
    dive_summaries: list[fitdecode.FitDataMessage] = field(default_factory=list)
    gases: list[fitdecode.FitDataMessage] = field(default_factory=list)
    tank_summaries: list[fitdecode.FitDataMessage] = field(default_factory=list)

    depth: list[tuple[datetime, float]] = field(default_factory=list)
    ceiling: list[tuple[datetime, int]] = field(default_factory=list)
    temperature: list[tuple[datetime, int]] = field(default_factory=list)
    # Keyed by the transmitter's ANT id, insertion-ordered so cylinders come out in the
    # order the device first reported them.
    pressure: dict[int, list[tuple[datetime, float]]] = field(default_factory=dict)
    # Kept as frames, unlike the sample channels: an `event` needs `_breathed_gases` to
    # resolve the cylinder it names, and that list isn't complete until the scan is.
    events: list[fitdecode.FitDataMessage] = field(default_factory=list)


class FitParser(DiveParser):
    """Parses ANT/Garmin FIT dive activity files (Garmin Descent, Suunto native export).

    Extracts the fields with a direct equivalent on the `Dive`/`DiveMixture` backend
    models (`models/dive.py`, `models/dive_mixture.py`), plus - separately, via
    `parse_profile` - the per-sample depth/ceiling/temperature/tank-pressure curves and
    the file's dive events, stored as `DiveProfile`. FIT activity files carry a great deal
    more (GPS track, ascent rates, heart rate, battery telemetry) with nowhere to persist
    it, so none of that is parsed.
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
        try:
            return cls._parse_dive(cls._scan(content))
        except EXTRACTION_ERRORS as exc:
            raise DiveParseError(f"Malformed FIT dive data: {exc}") from exc

    @classmethod
    def parse_profile(cls, content: bytes) -> ParsedProfileSchema | None:
        """Extract the `record` stream (and Garmin's `tank_update`s) as per-channel series."""
        try:
            return cls._parse_samples(cls._scan(content))
        except EXTRACTION_ERRORS as exc:
            raise DiveParseError(f"Malformed FIT dive samples: {exc}") from exc

    @classmethod
    def parse_all(cls, content: bytes) -> tuple[ParsedDiveSchema, ParsedProfileSchema | None]:
        """Both extractions off **one** decode, which is what this override buys.

        The two methods above each call `_scan`, so the pair costs two full passes over
        the file - measured at 55 ms + 58 ms on a 26 KB export where one scan feeding both
        is 58 ms, and scaling with `_MAX_FRAMES` to the ~1.5 s the profile extraction is
        budgeted at. The Suunto parsers are 2-11 ms and keep the base implementation.

        Nothing is given up by sharing the scan. `_scan` is a pure function of the bytes,
        so a file that fails it fails it for both entry points anyway - the independence
        the two methods appear to have at that level is already notional. What *is* real
        is a `_parse_dive` that raises where `_parse_samples` would not, and that is why
        the two interpretation steps below are guarded separately rather than under one
        `try`: this returns the same pair the two methods would have, one scan cheaper.

        `_parse_dive` and `_parse_samples` only read the scan - neither consumes it - so
        the order here is arbitrary and the results are identical to calling them apart.
        """
        # Guarded on its own terms, matching `parse()`: a scan failure is the one thing
        # that genuinely takes both halves, here and in the two methods above alike.
        try:
            scan = cls._scan(content)
        except EXTRACTION_ERRORS as exc:
            raise DiveParseError(f"Malformed FIT file: {exc}") from exc

        try:
            dive = cls._parse_dive(scan)
        except EXTRACTION_ERRORS as exc:
            raise DiveParseError(f"Malformed FIT dive data: {exc}") from exc

        try:
            samples = cls._parse_samples(scan)
        except EXTRACTION_ERRORS as exc:
            raise DiveParseError(f"Malformed FIT dive samples: {exc}") from exc

        return dive, samples

    @classmethod
    def _scan(cls, content: bytes) -> _FitScan:
        """Decode the file once, keeping the messages a dive is built from.

        `fitdecode`'s defaults are deliberately kept: `CrcCheck.WARN` and
        `ErrorHandling.WARN` mean a file whose checksum or trailing bytes are slightly
        off still imports. The CRC here guards against transfer corruption, not
        tampering - nothing downstream trusts it - and refusing an otherwise readable
        dive log over it would lose real data for no gain.

        **Catches `Exception`, not just `fitdecode.FitError`.** This is the one place a
        third-party decoder is walked over bytes a stranger uploaded, and a corrupt file
        does not reliably present as the library's own error type: flipping a few bytes
        past the header raises `AssertionError` from `reader.py`, `ValueError: size` from
        a bad field definition, and `TypeError: '>=' not supported between instances of
        'tuple' and 'int'` from `processors.py`. Every one of those escaped as a 500 from
        `POST /dive/parse`, which catches only the two parser errors. Truncation happens
        to raise `FitEOFError` - a real `FitError` - which is why the truncated-file test
        gave false confidence.
        """
        scan = _FitScan()
        try:
            with fitdecode.FitReader(io.BytesIO(content)) as fit:
                for count, frame in enumerate(fit, start=1):
                    if count > _MAX_FRAMES:
                        raise DiveParseError(
                            f"This FIT file holds more than {_MAX_FRAMES:,} records, which is far more "
                            "than any dive. It looks like an activity log rather than a dive log."
                        )
                    if isinstance(frame, fitdecode.FitDataMessage):
                        cls._collect(scan, frame)
        except DiveParseError:
            # Ours, and already phrased for the diver - not something the decoder threw.
            raise
        except Exception as exc:
            raise DiveParseError(f"Invalid FIT file: {str(exc) or type(exc).__name__}") from exc
        return scan

    @classmethod
    def _collect(cls, scan: _FitScan, frame: fitdecode.FitDataMessage) -> None:
        """Route one decoded message into the scan.

        Only the *first* `session` is kept. A dive export normally holds exactly one;
        where a device writes more (a multi-sport file, a repetitive-dive series), the
        first is the one this file is about, and `ParsedDiveSchema` describes a single
        dive either way.

        **Samples stop at that session, too.** A FIT file writes summary messages after
        the samples they summarize, so anything following the first `session` belongs to
        the next dive. Collecting the lot described the dive from session 1 while giving
        it a profile spanning the whole file: a two-dive file came back as 1 800 seconds
        deep 30 m, with a profile running to 7 260 s across a surface interval, so
        `DiveProfileInfo.duration_seconds` and the dive's own `duration` disagreed. The
        cut is positional rather than by the session's time window because `dive_gas`
        carries no timestamp to filter on, and it costs nothing on a real file: across
        the corpus the only message following the first `session` is the `activity`.
        """
        if frame.name == "session":
            scan.session_count += 1
            if scan.session is None:
                scan.session = frame
        elif frame.name == "activity":
            if scan.activity is None:
                scan.activity = frame
        elif frame.name == "dive_summary":
            # Cut at the *second* session rather than the first, unlike everything below:
            # a `dive_summary` is written after the session it refers to, so gating it on
            # the first would discard every one of them. Bounded all the same, because
            # `_dive_summary` prefers a summary whose `reference_mesg` names a session -
            # and with dive 1's summary omitting that field while dive 2's carries it,
            # the unbounded list handed dive 2's depths and bottom time to dive 1.
            if scan.session_count < 2:
                scan.dive_summaries.append(frame)
        elif frame.name == "tank_summary":
            # Above the guard for exactly the reason `dive_summary` is: it summarizes the
            # dive rather than sampling it, so a device may write it after the `session`
            # - and gating it there dropped both pressures, which is the whole SAC/RMV.
            # Nothing in the corpus could have caught this: no file in it has any tank
            # telemetry, so "the only message after the first session is the activity"
            # was measured on files with no `tank_summary` to place. Bounded at the
            # second session all the same.
            if scan.session_count < 2 and len(scan.tank_summaries) < _MAX_CYLINDERS:
                scan.tank_summaries.append(frame)
        elif scan.session is not None:
            # Samples and gas for a dive this file describes after the one being imported.
            return
        elif frame.name == "dive_gas":
            scan.gases.append(frame)
        elif frame.name == "record":
            cls._collect_record(scan, frame)
        elif frame.name == "tank_update":
            cls._collect_tank_update(scan, frame)
        elif frame.name == "event" and _native_value(frame, "event") in _EVENT_TYPE_BY_NAME:
            # Filtered at collection rather than at interpretation, which the cylinder
            # paths above deliberately do the other way round. There the filter is on a
            # field (`status`) that decides between real cylinders, so filtering early
            # would let sixteen disabled entries crowd out the ones breathed. Here the
            # filter *is* the vocabulary - a `timer` or a `battery_low` can never become an
            # event - so keeping them would only let the shared cycling and running alerts
            # in this enum consume the cap below.
            if len(scan.events) < _MAX_EVENTS:
                scan.events.append(frame)

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

        # `next_stop_depth` is FIT's deco ceiling: the depth of the next required stop, in
        # meters, scaled by the profile like `depth` beside it. **Not** `next_stop_time`,
        # `time_to_surface` or `ndl_time`, which are the three neighbouring fields that
        # measure durations rather than a depth. Unattested in this corpus - no file in it
        # writes the field at all - which is the same position `tank_update` was in when it
        # was implemented, and it is tested the same way, through `tests/helpers/fit.py`.
        ceiling = ceiling_cm(_native_value(frame, "next_stop_depth"))
        if ceiling is not None:
            scan.ceiling.append((timestamp, ceiling))

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
        # Raw, not rendered: an ANT id of 65535 decodes to the string
        # "ant_device_number" through the profile's bitfield enum. See `_native_raw`.
        sensor = _native_raw(frame, "sensor")
        if not isinstance(timestamp, datetime) or pressure is None or sensor is None:
            return
        key = int(sensor)
        # Every distinct ANT id becomes a mixture in the `/dive/parse` response and a
        # channel in the stored profile, and nothing else bounded how many there could
        # be: `_MAX_FRAMES` caps frames, `MAX_POINTS_PER_CHANNEL` caps points *within* a
        # channel. A 1 MB file of `tank_update` records with unique sensors produced
        # 99 000 mixtures and a 9.9 MB parse response - a ~9x amplification.
        if key not in scan.pressure and len(scan.pressure) >= _MAX_CYLINDERS:
            return
        scan.pressure.setdefault(key, []).append((timestamp, pressure))

    @staticmethod
    def _dive_summary(scan: _FitScan) -> fitdecode.FitDataMessage | None:
        """The `dive_summary` describing the whole activity, not one dive inside it.

        A Garmin freediving activity writes a `dive_summary` per individual descent
        *plus* a session-level one, and `reference_mesg` says which is which - it names
        the message type the summary refers to, `session` or `lap`. Taking the first one
        seen would read a single descent's depth and bottom time as the whole dive's.

        Falls back to the first summary of any kind, since a single-dive export commonly
        writes one without a `reference_mesg` at all.
        """
        for summary in scan.dive_summaries:
            if _native_value(summary, "reference_mesg") == "session":
                return summary
        return scan.dive_summaries[0] if scan.dive_summaries else None

    @classmethod
    def _parse_dive(cls, scan: _FitScan) -> ParsedDiveSchema:
        """Map the scanned messages onto `ParsedDiveSchema`."""
        session = cls._dive_session(scan)
        summary = cls._dive_summary(scan)

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
            # Both messages carry the same three fields, and `_summary_field` prefers
            # Garmin's `dive_summary` for the same reason `_depth` does.
            #
            # `end_cns` is already whole percent (the profile's own unit), unlike the
            # Suunto JSON export's 0-1 fraction. There is no `start_otu` anywhere in the
            # FIT profile, so `otu_start` stays null rather than being back-derived.
            #
            # **`o2_toxicity` is the dive's ending OTU total, not the OTUs it added**,
            # which the profile's bare "OTUs" unit does not settle. The corpus does: the
            # 2025-03-06 08:29 dive exists as both a FIT and a DM5 XML export, and where
            # the XML records `OtuStart 22 -> OtuEnd 23`, the FIT writes
            # `o2_toxicity = 23`. Read as a delta it would have been 1.
            cns_start=cls._summary_field(session, summary, "start_cns"),
            cns_end=cls._summary_field(session, summary, "end_cns"),
            otu_start=None,
            otu_end=cls._summary_field(session, summary, "o2_toxicity"),
            # FIT records no surface pressure for a dive. `record.absolute_pressure` is
            # the *ambient* pressure at each sample, so the closest thing available would
            # be "whatever the first sample read", which is a guess about when the diver
            # entered the water rather than a barometer reading.
            surface_pressure_bar=None,
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
    def _summary_field(
        session: fitdecode.FitDataMessage, summary: fitdecode.FitDataMessage | None, name: str
    ) -> float | None:
        """A field carried by both `dive_summary` and `session`, preferring the summary.

        The mirror of `_depth`, which prefers the session and falls back to the summary.
        The order is the other way round here because these are the *dive's* oxygen
        accounting: on a multi-dive Garmin file the session totals cover the whole
        activity, whereas `_dive_summary` has already picked out the summary that
        describes the dive being imported.
        """
        value = _native_value(summary, name) if summary is not None else None
        if value is None:
            value = _native_value(session, name)
        return float(value) if value is not None else None

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
        """Map `dive_gas` entries onto `DiveMixture`s, with tank pressures where present.

        Cylinders are numbered by their 1-based position in this list, which is the same
        ordering `_cylinder_sensors` numbers the profile's pressure channels by - so a
        pod sits at the same `gas_number` on the chart legend as on the dive form.

        **That correspondence is only as good as the pairing underneath it.** Where a
        file has two gases and one pod, `_tanks_for` refuses to guess which is which and
        leaves both pressures null, but the mixtures are still numbered 1 and 2 while the
        only pressure channel is numbered 1 - so the numbers line up by position without
        the file having said they describe the same cylinder. The number is a label, and
        anything joining on it (per-tank gas accounting) has to treat it as one.
        """
        gases = cls._breathed_gases(scan)
        if not gases:
            # Tank telemetry with no gas list at all still describes real cylinders - a
            # Descent dive logged in gauge mode writes no `dive_gas`, and a pod paired to
            # it reports throughout. Emitting the pressures on their own is the same rule
            # the JSON parser follows in this file's sibling: evidence of a tank is
            # evidence of a tank, whichever way round it arrived. Returns `[]` when there
            # is no telemetry either.
            return [
                cls._mixture(None, tank, gas_number) for gas_number, tank in enumerate(cls._tank_pressures(scan), 1)
            ]

        return [
            cls._mixture(gas, tank, gas_number)
            for gas_number, (gas, tank) in enumerate(zip(gases, cls._tanks_for(scan, gases), strict=True), start=1)
        ]

    @staticmethod
    def _breathed_gases(scan: _FitScan) -> list[fitdecode.FitDataMessage]:
        """The `dive_gas` entries for cylinders the diver actually breathed, device order first.

        A FIT device stores its whole configured gas list, so both `disabled` and
        `backup_only` are dropped - a `backup_only` cylinder is by definition one that was
        carried and not breathed, and importing a pony bottle as a second mixture costs the
        dive its SAC/RMV, since `compute_gas_use` requires exactly one.

        Deduped on `message_index` keeping the first, since a device may re-announce its
        list mid-file. Entries *without* an index are kept in a separate space and appended
        rather than being keyed by their position: keying a position into the same dict as
        a real `message_index` made a gas at position 0 collide with a gas declaring
        `message_index=0` - one of the two vanished - and then sorted positions and indices
        together as though the two numbers were on one scale.
        """
        indexed: dict[int, fitdecode.FitDataMessage] = {}
        unindexed: list[fitdecode.FitDataMessage] = []
        for gas in scan.gases:
            if _native_value(gas, "status") in _UNUSED_GAS_STATUSES:
                continue
            index = _native_raw(gas, "message_index")
            if index is None:
                unindexed.append(gas)
            else:
                indexed.setdefault(int(index) & _MESSAGE_INDEX_MASK, gas)

        # Capped like the tank paths, and for the same reason: every entry becomes a
        # `DiveMixtureSchema` in the `/dive/parse` response. `dive_gas` is 2 bytes of
        # payload, so `_MAX_FRAMES` alone let a 220 KB file return 20 000 mixtures - a
        # bigger amplification through the same field than the `tank_update` case this
        # constant was introduced for. Truncated at the end rather than at collection:
        # `status` is filtered above, so a collect-time `len(scan.gases)` bound would let
        # sixteen disabled entries crowd out the gases actually breathed.
        return ([indexed[index] for index in sorted(indexed)] + unindexed)[:_MAX_CYLINDERS]

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
    def _cylinder_sensors(scan: _FitScan) -> list[int]:
        """The pods this dive has, in the order a cylinder is numbered in.

        One ordering for both places a cylinder gets a position: the mixture list on the
        dive form, and the `gas_number` labels on the profile's pressure curves. They
        used to be computed separately - mixtures summary-first, channels by the order
        pods started streaming - so a device enumerating its summaries in a different
        order than its telemetry arrived made the chart's "Gas 1" and the form's first
        cylinder describe different tanks.

        Summaries first, being the device's own enumeration of its pods, then any pod
        that only ever streamed telemetry.
        """
        summary_sensors: list[int] = []
        for summary in scan.tank_summaries:
            sensor = _native_raw(summary, "sensor")
            if sensor is None or int(sensor) in summary_sensors:
                continue
            # Naming a pod is not on its own evidence of a cylinder: a summary with no
            # pressures earns a place only if that pod also streamed telemetry to merge
            # in. Otherwise a file whose sole tank message is `tank_summary(sensor=...,
            # volume_used=...)` grows an entirely null phantom cylinder on the form.
            has_pressure = (
                _native_value(summary, "start_pressure") is not None
                or _native_value(summary, "end_pressure") is not None
            )
            if has_pressure or int(sensor) in scan.pressure:
                summary_sensors.append(int(sensor))
        return (summary_sensors + [sensor for sensor in scan.pressure if sensor not in summary_sensors])[
            :_MAX_CYLINDERS
        ]

    @classmethod
    def _tank_pressures(cls, scan: _FitScan) -> list[_TankPressures]:
        """What each cylinder started and finished the dive on.

        Two sources, joined **per cylinder and per field** rather than one taking over from
        the other. `tank_summary` is the device's own figure and wins where it has one;
        anything it leaves null falls through to the ends of that pod's `tank_update`
        telemetry - the first and last reading it sent - since a Descent streams
        `tank_update` throughout the dive whether or not it also emits a summary. Readings
        are ordered by their own timestamps rather than by arrival, since pods interleave.

        Falling through per *branch* - "if there are any summaries at all, ignore the
        telemetry" - looked equivalent and wasn't: it keyed off a frame existing rather
        than that frame carrying numbers. The realistic case is the partial one. A pod that
        drops out near the end writes a summary with `start_pressure` set and
        `end_pressure` null, and the last real reading - the one the whole SAC/RMV turns on
        - was discarded in favour of that null. Transmitter dropout is routine rather than
        hypothetical; `suunto_xml.py` records 224 of 441 samples missing it in the corpus.

        The join is exact rather than positional: both messages carry the pod's ANT
        `sensor` id, so this is a real key and not the kind of guess `_tanks_for` refuses
        to make between tanks and gases.

        Summaries are **deduped by `sensor`, keeping the last**, the same way telemetry is
        grouped by it. A device that writes the summary twice for one pod would otherwise
        count as two cylinders, and `_tanks_for`'s exact-count rule then discards every
        pressure in the file - one repeated frame losing a real 207 -> 62 bar and the
        dive's RMV with it.

        A summary with no `sensor` cannot be joined to anything, so it stands as its own
        cylinder - unless it carries no pressures either, in which case it describes
        nothing at all and is dropped rather than inflating the count past the gas list.

        **The telemetry is deliberately not bounded to the in-water part of the dive, and
        that is a known gap rather than a considered non-problem.** `SuuntoJsonParser`
        bounds its equivalent on `Header.DiveTime` because two dives in that corpus ended
        with a purged regulator reporting 0.14 bar instead of 53 and 76 - a diver who
        breathed their cylinder dry, as far as `compute_gas_use` is concerned. The same
        surface tail exists here: both Ocean files log for ~5 minutes past the last reading
        deeper than 1.2 m (308 s and 302 s), matching the 343 s gap between `DiveTime` and
        `Duration` in the JSON export of a comparable dive.

        What FIT lacks is anywhere to read the in-water time from. `total_elapsed_time`,
        `total_timer_time` and `session.timestamp - start_time` are all 4 302 s on the same
        dive - the whole logged period, tail included - and the depth channel keeps
        reporting 0.0 m right up to the session end, so "the last depth sample" is not an
        end-of-dive marker either. The only bound left would be a depth threshold this
        module invented, which is the kind of guess `_tanks_for` refuses to make two
        methods down.

        So it is left unbounded, and this comment exists so the next reader does not
        re-derive the question. To settle it, take a Descent export with a pod on it and
        compare the last `tank_update` against the last record deeper than a metre: a tail
        of surface readings means this needs the same treatment as the JSON parser, and
        `dive_summary.bottom_time` plus `descent_time`/`ascent_time` are the fields most
        likely to reconstruct a window. No file in the corpus has any tank telemetry at
        all, so none of this can be checked here.
        """
        telemetry: dict[int, _TankPressures] = {}
        for sensor, unordered in scan.pressure.items():
            readings = sorted(unordered, key=lambda reading: reading[0])
            if readings:
                telemetry[sensor] = _TankPressures(start=readings[0][1], end=readings[-1][1])

        summaries: dict[int, _TankPressures] = {}
        unidentified: list[_TankPressures] = []
        for summary in scan.tank_summaries:
            pressures = _TankPressures(
                start=_native_value(summary, "start_pressure"),
                end=_native_value(summary, "end_pressure"),
            )
            summary_sensor = _native_raw(summary, "sensor")
            if summary_sensor is not None:
                # Merged per field rather than last-frame-wins. Overwriting made the
                # dedup order-dependent in exactly the way its own docstring says it
                # exists to prevent: a bare `volume_used` repeat *after* a real summary
                # wiped a genuine 207 -> 62 bar, while the same two frames the other way
                # round kept it. `_cylinder_sensors` admits the cylinder on whichever
                # frame carries pressures, so this is also what keeps the two agreeing.
                #
                # Kept unfiltered otherwise: `_cylinder_sensors` decides which of these
                # earn a cylinder, so that rule lives in one place and the profile's
                # channel numbering can't disagree with the mixture list about it.
                summaries[int(summary_sensor)] = _merge_pressures(
                    summaries.get(int(summary_sensor), _NO_PRESSURES), pressures
                )
            elif pressures != _NO_PRESSURES:
                unidentified.append(pressures)

        # Summary order first - the device's own enumeration of its pods - then any pod
        # that only ever streamed telemetry.
        sensors = cls._cylinder_sensors(scan)
        return (
            [
                _merge_pressures(summaries.get(sensor, _NO_PRESSURES), telemetry.get(sensor, _NO_PRESSURES))
                for sensor in sensors
            ]
            + unidentified
        )[:_MAX_CYLINDERS]

    @staticmethod
    def _mixture(
        gas: fitdecode.FitDataMessage | None, tank: _TankPressures | None, gas_number: int
    ) -> DiveMixtureSchema:
        """Map one `dive_gas` (plus its cylinder's pressures, if any) onto a `DiveMixture`.

        No unit conversion: the FIT profile already defines `oxygen_content`/
        `helium_content` as whole percent and tank pressures as bar, which is what the
        model stores.

        `gas` is `None` for a cylinder known only from its transmitter, which is a file
        with tank telemetry and no gas list - the mixture then carries pressures and
        nothing else.
        """
        oxygen = _native_value(gas, "oxygen_content") if gas is not None else None
        helium = _native_value(gas, "helium_content") if gas is not None else None
        return DiveMixtureSchema(
            end_pressure=tank.end if tank is not None else None,
            gas_number=gas_number,
            helium=float(helium) if helium is not None else None,
            # Left for the user to fill in themselves rather than parsed - see
            # DECISIONS.md.
            name=None,
            oxygen=float(oxygen) if oxygen is not None else None,
            # `dive_gas` has no ppO2 field in the FIT profile at all - the limits live on
            # `dive_settings` (`po2_warn`/`po2_critical`), which are the *device's*
            # thresholds for the whole dive rather than this cylinder's plan. Reading one
            # of those into a per-mixture column would attribute a global setting to
            # every gas, so it stays null and the client falls back to its own default.
            po2_limit=None,
            role=_role(gas),
            start_pressure=tank.start if tank is not None else None,
            # FIT has nowhere to record cylinder size at all - not on `dive_gas`, and
            # `tank_summary` carries only the volume *consumed*. `None`, not 0.0: the
            # format cannot express this, so the dive form applies its own
            # `DEFAULT_MIXTURE` rather than being handed a cylinder of no volume.
            volume=None,
        )

    @classmethod
    def _events(cls, scan: _FitScan, elapsed: Callable[[datetime], float]) -> list[ParsedProfileEvent]:
        """The `event` messages that describe the dive, as chart markers.

        The one that needs work is `dive_gas_switched`, whose `data` field holds the
        switched-to gas's **`message_index`** - the device's own key for a `dive_gas` entry,
        which is not the `gas_number` anything downstream uses. Mixtures are numbered by
        position in `_breathed_gases`, so the index is resolved through that same list and
        a marker lands on the cylinder the dive form shows.

        Where it cannot be resolved the marker is still emitted, with `gas_number` left
        null. A switch to a gas whose `dive_gas` was `disabled` (so `_breathed_gases`
        dropped it) or absent is a real switch that happened; saying "a gas switch, to
        something this file doesn't describe" is honest, where guessing a position would be
        the join-by-hope `_tanks_for` refuses two methods up.
        """
        positions = {
            int(index) & _MESSAGE_INDEX_MASK: position
            for position, index in enumerate(
                (_native_raw(gas, "message_index") for gas in cls._breathed_gases(scan)), start=1
            )
            if index is not None
        }

        events: list[ParsedProfileEvent] = []
        for frame in scan.events:
            timestamp = _native_value(frame, "timestamp")
            name = _native_value(frame, "event")
            event_type = _EVENT_TYPE_BY_NAME.get(name) if isinstance(name, str) else None
            if not isinstance(timestamp, datetime) or event_type is None:
                continue
            data = _native_value(frame, "data")
            gas_number = positions.get(int(data) & _MESSAGE_INDEX_MASK) if isinstance(data, int) else None
            events.append(
                ParsedProfileEvent(
                    t=elapsed(timestamp),
                    type=event_type,
                    gas_number=gas_number if event_type is ProfileEventType.GAS_SWITCH else None,
                    # `data` renders through the profile's own `dive_alert` enum for that
                    # event, so this is the device's wording - `deco_ceiling_broken`, not a
                    # number. An alert whose code is outside the enum decodes to the bare
                    # integer, which is still more than "something happened".
                    label=str(data) if event_type is ProfileEventType.OTHER and data is not None else None,
                )
            )
        # An `OTHER` with nothing to say is dropped rather than failing the extraction:
        # `_validate_events` rejects it, and a file whose one unreadable alert took its
        # depth curve down with it would be the opposite of what that rule is for.
        return [event for event in events if event.type is not ProfileEventType.OTHER or event.label]

    @staticmethod
    def _earliest_sample(scan: _FitScan) -> datetime:
        """The first reading on any channel, for a file whose `session` has no start time.

        Only reached when there is at least one reading, which `_parse_samples` has
        already established.

        Streams into `min` rather than building the list first: a long Ocean dive is ~4 300
        records plus telemetry, and this wants one value out of all of them.
        """
        return min(
            chain(
                (timestamp for timestamp, _ in scan.depth),
                (timestamp for timestamp, _ in scan.ceiling),
                (timestamp for timestamp, _ in scan.temperature),
                *((timestamp for timestamp, _ in readings) for readings in scan.pressure.values()),
            )
        )

    @classmethod
    def _parse_samples(cls, scan: _FitScan) -> ParsedProfileSchema | None:
        """Turn the scanned sample streams into one series per channel.

        Timestamps are rebased onto the dive's start as fractional seconds. The origin is
        the `session` start where there is one, so every channel shares an axis; absolute
        zero doesn't matter (`services/dive_profiles.py` rebases onto the earliest
        reading across all channels anyway), only that the channels agree on it.
        """
        if not scan.depth and not scan.temperature and not scan.pressure and not scan.ceiling:
            return None

        start_time = _native_value(scan.session, "start_time") if scan.session is not None else None
        origin: datetime = start_time if isinstance(start_time, datetime) else cls._earliest_sample(scan)

        def elapsed(timestamp: datetime) -> float:
            return (timestamp - origin).total_seconds()

        return ParsedProfileSchema(
            depth=series([(elapsed(t), scaled_int(v, CENTIMETERS_PER_METER)) for t, v in scan.depth]),
            # Already in centimeters: `ceiling_cm` scaled it at collection, since that is
            # also where a zero was ruled out as "no ceiling".
            ceiling=series([(elapsed(t), v) for t, v in scan.ceiling]),
            temperature=series([(elapsed(t), scaled_int(v, TENTHS_PER_UNIT)) for t, v in scan.temperature]),
            # Labelled 1, 2, ... in the order the device first reported each pod, never by
            # `sensor` - that is an ANT id (a serial, e.g. 2411100050), so it would read as
            # nonsense in a chart legend. Same reasoning as the XML parser's refusal to use
            # `<TransmitterId>`, and it keeps a single-cylinder dive labelled gas 1 there
            # and here alike.
            # Numbered by position in `_cylinder_sensors`, the same ordering the
            # mixtures use, so a pod sits at the same index on the chart legend as on
            # the dive form. A sensor with a summary but no telemetry holds its place in
            # the numbering without contributing a curve.
            pressure=[
                ParsedPressureSeries(gas_number=number, t=channel.t, v=channel.v)
                for number, channel in (
                    (
                        position,
                        series(
                            [(elapsed(t), scaled_int(v, TENTHS_PER_UNIT)) for t, v in scan.pressure.get(sensor, [])]
                        ),
                    )
                    for position, sensor in enumerate(cls._cylinder_sensors(scan), start=1)
                )
                if channel is not None
            ],
            events=cls._events(scan, elapsed),
        )
