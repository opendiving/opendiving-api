"""Recordings: what one device recorded of one dive, and how two of them are matched.

The **only** module that decides whether two records are the same recording, the same
dive, or neither. Two halves, on the `dive_files.py` idiom: the top one is pure and DB-free
(`same_device`, `devices_differ`, `delta_seconds`, the three gates), so the decisions worth
testing are testable without a database; the bottom one persists.

**Why any of this exists.** A dive used to have at most one export and at most one profile.
Three ordinary things a diver does break that: wearing two computers, exporting one computer
twice (the same Suunto Ocean writes a JSON and a FIT of one dive, and neither carries what
the other does), and surfacing mid-dive so a computer logs two records. A recording is the
row between the dive and its files that makes all three representable.

**Every match is within one account.** Candidates come from `dive_recording` by
`(user_id, start_time)` and nothing here ever looks at another diver's rows.
"""

import logging
import uuid as uuid_pkg
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from ..core.utils.datetime_offset import combine_start_time
from ..models.dive import Dive
from ..models.dive_file import DiveFile
from ..models.dive_profile import DiveProfile
from ..models.dive_recording import DiveRecording
from ..schemas.dive import DiveFileInfo, RecordingDevice, RecordingRead
from ..schemas.parsed_dive import ParsedDevice
from . import blob_store
from .dive_profiles import get_profile_infos_for_recordings

logger = logging.getLogger(__name__)

# The six device columns, in one place: the read schema, the fill rule and the row writes
# all walk this rather than repeating the list, so a seventh member added to `ParsedDevice`
# is added here and nowhere else. Keyed member name -> column name, because the two differ
# by a prefix and the prefix is what keeps `dive_recording` readable in `psql`.
DEVICE_COLUMNS: dict[str, str] = {
    "brand": "device_brand",
    "model": "device_model",
    "serial": "device_serial",
    "firmware": "device_firmware",
    "name": "device_name",
    "dive_number": "device_dive_number",
}

# |Δ start| admitting a second *file of the same recording*. Two seconds, because the same
# computer's two exports of one dive disagree by rounding and nothing else: the corpus's
# Ocean writes `15:17:38.67` in its JSON and `15:17:38` in its FIT.
SAME_RECORDING_START_TOLERANCE = 2.0
# And the same tolerance on the sampled span, for the same reason - two readings of one
# sample stream, not two dives.
SAME_RECORDING_SPAN_TOLERANCE = 2.0

# The strict gate's numbers are Subsurface's `likely_same` (`core/dive.cpp`), adopted as
# they are rather than re-derived: a floor of a minute on the start delta, widening to half
# the longer dive so that two computers whose clocks drifted across a long dive still pair;
# depths within a tenth or a metre; durations within five minutes.
STRICT_START_FLOOR = 60.0
STRICT_DEPTH_FRACTION = 0.1
STRICT_DEPTH_ABSOLUTE = 1.0
STRICT_DURATION_TOLERANCE = 300.0

# How far either side of an incoming start the candidate query reaches: twelve hours of
# fuzz, wider than any gate above can admit, plus the fourteen an offset can move a wall
# clock from its instant. Wide on purpose - the query is an index scan on
# `(user_id, start_time)` and the gates do the real work in Python, so the cost of being
# generous here is a few rows and the cost of being tight is a match that silently never
# fires for a diver whose computer is set to the wrong side of the date line.
CANDIDATE_WINDOW = timedelta(hours=26)


class RecordingNotFoundError(Exception):
    """No recording of this dive under that uuid."""


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    """A device as some file named it, for comparison only.

    Built from a `ParsedDevice` (the attach path), from a `dive_recording` row (the stored
    side) or from an imported document's device. `firmware` and `name` are deliberately
    absent: neither distinguishes two computers - firmware changes on one machine and a
    name is whatever its owner typed - so carrying them here would invite a comparison that
    reads as evidence and is not.
    """

    brand: str | None = None
    model: str | None = None
    serial: str | None = None
    dive_number: int | None = None

    @property
    def is_empty(self) -> bool:
        """Whether this describes nothing - which is what an older document's recording has.

        Not the same as "no serial": a device carrying only a brand is a real, weak claim
        and the gates treat it as one. This is the case where the *file* said nothing at
        all, and the same-recording gate falls back to start and span for it.
        """
        return self.brand is None and self.model is None and self.serial is None


def _folded(value: str | None) -> str | None:
    """Trimmed and case-folded, or `None`.

    Every device string is compared this way and stored as read. A FIT's
    `file_id.manufacturer` decodes to the profile's lowercase `suunto` while the same
    computer's app JSON writes the literal `Suunto`, so a case-sensitive comparison would
    read one machine's two exports as two machines - which is the exact defect a recording
    exists to prevent.
    """
    if value is None:
        return None
    return value.strip().casefold() or None


def device_of(source: ParsedDevice | None) -> DeviceIdentity:
    """A parse's device as an identity. An absent device is an empty identity, not `None`,
    so every caller compares the same shape."""
    if source is None:
        return DeviceIdentity()
    return DeviceIdentity(brand=source.brand, model=source.model, serial=source.serial, dive_number=source.dive_number)


def same_device(left: DeviceIdentity, right: DeviceIdentity) -> bool:
    """Whether these two files describe the same computer.

    **One rule underlies this and `devices_differ` below: a member absent on either side
    never makes two devices differ.** Only a member *both* sides carry, whose values
    disagree, does. That is not leniency - it is what the formats force. The `.ssrf` reader
    reports no brand at all and UDDF's `<manufacturer>` is optional, so without it an
    `.ssrf` `{model: "Suunto Ocean"}` and that same computer's FIT
    `{brand: "suunto", model: "Suunto Ocean"}` would be neither the same nor different, and
    "a different device" is the strict gate's entry condition - so the gate would either
    never fire or mint a second recording for one computer.

    Two devices are the same when both carry a serial and the serials are equal, or when at
    most one carries a serial and the models are equal or either is unknown. The brands
    must not disagree in either branch, which an absent brand never does.

    So the corpus's Suunto JSON (serial `253810000400`, brand `Suunto`, no model) and its
    FIT (no serial, brand `suunto`, model `Suunto Ocean`) are the same device: one serial in
    play, brands equal folded, one model unknown.
    """
    if _disagree(left.brand, right.brand):
        return False

    left_serial, right_serial = _folded(left.serial), _folded(right.serial)
    if left_serial is not None and right_serial is not None:
        return left_serial == right_serial
    return not _disagree(left.model, right.model)


def devices_differ(left: DeviceIdentity, right: DeviceIdentity) -> bool:
    """Whether these two files describe *different* computers - the strict gate's entry.

    Not `not same_device(...)`. The two are deliberately not complements: a pair may be
    neither, which is what two devices with nothing comparable between them are, and
    reading "not the same" as "different" would let the strict gate fire on a pair it knows
    nothing about.

    Different means both carry serials that differ, both carry brands that differ, or -
    **with at most one serial in play** - both carry models that differ. That last
    qualifier is load-bearing rather than defensive: without it, equal serials with
    differing model strings would satisfy *same* and *different* at once, and a real pair
    produces exactly that (a Shearwater UDDF's `<model>Perdix 3</model>` beside the same
    computer's `.ssrf` `@model='Shearwater Perdix 3'`, both serial `D9772626`). A serial
    both sides carry settles the question either way.
    """
    if _disagree(left.brand, right.brand):
        return True

    left_serial, right_serial = _folded(left.serial), _folded(right.serial)
    if left_serial is not None and right_serial is not None:
        return left_serial != right_serial
    return _disagree(left.model, right.model)


def _disagree(left: str | None, right: str | None) -> bool:
    """Whether both sides carry this member and the two values are not equal."""
    folded_left, folded_right = _folded(left), _folded(right)
    return folded_left is not None and folded_right is not None and folded_left != folded_right


def wall_clock(start_time: datetime, utc_offset_minutes: int | None) -> datetime:
    """The clock face the diver would have read, from the stored column pair.

    An offset-less recording already holds its wall clock in `start_time`, labelled UTC
    because a `timestamptz` has nowhere else to put it (see `core/utils/datetime_offset.py`),
    so that column *is* the answer. One that carries an offset holds a real instant, and the
    wall clock is that instant shifted by it.
    """
    if utc_offset_minutes is None:
        return start_time
    return start_time + timedelta(minutes=utc_offset_minutes)


def delta_seconds(
    left_start: datetime,
    left_offset: int | None,
    right_start: datetime,
    right_offset: int | None,
) -> float:
    """|Δ| between two recordings' starts, measured on whichever clock both can speak.

    **Between instants when both sides carry an offset, and between wall clocks when either
    does not.** This is the single most consequential line in the module and it is not a
    fallback: Shearwater Cloud Desktop writes a local wall clock with a `Z` suffix, so a
    Perdix recording converted from its UDDF carries no offset at all and its `start_time`
    holds `15:18:10` labelled UTC - while the Suunto that was on the same diver's other
    wrist stamps `15:17:38+03:00`, an instant of `12:17:38Z`. Comparing the two columns
    naively gives 11 055 seconds and no gate ever fires. The one thing a device without an
    offset and a device with one share is the clock face their divers set, so that is what
    is compared whenever an offset is missing.

    The rejected alternative is converting a wall clock to an instant using the account's or
    the dive's offset. That fabricates the offset DiveJSON §5.2 forbids inventing, and
    writes it into a comparison where it would be invisible.
    """
    if left_offset is not None and right_offset is not None:
        return abs((left_start - right_start).total_seconds())
    return abs((wall_clock(left_start, left_offset) - wall_clock(right_start, right_offset)).total_seconds())


@dataclass(frozen=True, slots=True)
class RecordingFacts:
    """One side of a match: what it was recorded by, when, and the two figures the strict
    gate compares.

    `duration` and `max_depth` are **the recording's own**, never the dive's, and their
    provenance is the path that produced them - the device's logged figures on the attach
    path, the samples' own span and deepest reading on the import path. `sampled_span` is
    separately the profile's span, which the same-recording gate compares and the strict
    gate does not: the same file is 3 051 logged seconds and 3 473 sampled ones, so the two
    questions need two numbers.
    """

    device: DeviceIdentity
    start_time: datetime
    utc_offset_minutes: int | None = None
    duration: int | None = None
    max_depth: float | None = None
    sampled_span: int | None = None


@dataclass(frozen=True, slots=True)
class RecordingCandidate:
    """A stored recording the gates may match against, plus what a form needs to name it."""

    id: int
    uuid: uuid_pkg.UUID
    dive_id: int
    dive_uuid: uuid_pkg.UUID
    dive_number: int
    ordinal: int
    facts: RecordingFacts


def is_same_recording(incoming: RecordingFacts, stored: RecordingFacts) -> bool:
    """Whether these are two files of one record - the second-file case.

    The same device, starts within two seconds, device counters equal where both carry one,
    and sampled spans within two seconds where both have samples. The corpus's JSON and FIT
    of one Ocean dive pass on every clause (0.67 s apart, 3 473 s each); the two halves of
    the interrupted Perdix dive fail on the start (223 s) and on their spans (180 s against
    2 940), neither carrying a counter - Shearwater Cloud writes the diver's `<divenumber>`,
    not the computer's `<internaldivenumber>`.

    **A stored recording that names no device at all is matched on start and span alone.**
    That is not a weakening: it is the row a logbook import created before this table
    existed, or from a document whose source recorded nothing about the computer, and
    refusing to fill it would leave a diver unable to attach the very file that would say
    what recorded it. The residual risk is two computers of unknown model started within two
    seconds *and* recording spans within two seconds of each other, which the second clause
    makes remote and which the attach form's own choice makes harmless.
    """
    if not stored.device.is_empty and not same_device(incoming.device, stored.device):
        return False
    if (
        incoming.device.dive_number is not None
        and stored.device.dive_number is not None
        and incoming.device.dive_number != stored.device.dive_number
    ):
        return False
    if _start_delta(incoming, stored) > SAME_RECORDING_START_TOLERANCE:
        return False
    if incoming.sampled_span is not None and stored.sampled_span is not None:
        return abs(incoming.sampled_span - stored.sampled_span) <= SAME_RECORDING_SPAN_TOLERANCE
    return True


def is_same_dive_strict(incoming: RecordingFacts, stored: RecordingFacts) -> bool:
    """Whether these are two devices' records of one dive - the gate import attaches on.

    Subsurface's `likely_same`, with its own numbers: a different device (or the same one
    reporting a different counter, which is one computer's two dives), starts within
    `max(60 s, half the longer duration)`, maximum depths within 10 % or 1 m, and durations
    within five minutes.

    **The absent rule here is Subsurface's, not this module's, and it differs from
    `same_device`'s.** A figure *neither* side carries does not stop a match; a figure one
    side carries alone does - because a recording claiming 45 m against one claiming nothing
    is not evidence of agreement. `likely_same` additionally refuses a pair when either
    duration is zero, which is kept: a zero-length record is a device that logged nothing,
    and nothing is not a match.
    """
    if not devices_differ(incoming.device, stored.device):
        same_counter = incoming.device.dive_number == stored.device.dive_number
        both_counted = incoming.device.dive_number is not None and stored.device.dive_number is not None
        if not (both_counted and not same_counter):
            # The same device with the same counter (or with none) is one computer's record
            # of one dive. Two of those are the *merge* action's case - a dive interrupted
            # mid-water - and never a second recording of one dive.
            return False

    if not _within(incoming.duration, stored.duration, STRICT_DURATION_TOLERANCE):
        return False
    if incoming.duration == 0 or stored.duration == 0:
        return False
    if not _within_depth(incoming.max_depth, stored.max_depth):
        return False

    longer = max(incoming.duration or 0, stored.duration or 0)
    return _start_delta(incoming, stored) <= max(STRICT_START_FLOOR, longer / 2)


def is_same_dive_loose(incoming: RecordingFacts, stored: RecordingFacts) -> bool:
    """The start window alone - the candidates a form offers, where the diver decides.

    Deliberately the strict gate's *start* clause with nothing else attached. A form is
    asking "did you mean one of these?", and a depth or duration clause there would silently
    withhold the dive a diver is looking straight at because their second computer surfaced
    early.
    """
    longer = max(incoming.duration or 0, stored.duration or 0)
    return _start_delta(incoming, stored) <= max(STRICT_START_FLOOR, longer / 2)


def _start_delta(incoming: RecordingFacts, stored: RecordingFacts) -> float:
    return delta_seconds(incoming.start_time, incoming.utc_offset_minutes, stored.start_time, stored.utc_offset_minutes)


def _within(left: int | None, right: int | None, tolerance: float) -> bool:
    """Subsurface's absent rule: neither side carrying it is fine, one side alone is not."""
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    return abs(left - right) <= tolerance


def _within_depth(left: float | None, right: float | None) -> bool:
    """Depths agree within a tenth or a metre - whichever is the more generous.

    The absolute metre is what makes the fraction usable in shallow water: 10 % of a 4 m
    training dive is 40 cm, which two computers on one wrist routinely disagree by.
    """
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    return abs(left - right) <= max(STRICT_DEPTH_ABSOLUTE, max(left, right) * STRICT_DEPTH_FRACTION)


# ---------------------------------------------------------------- persistence


def _facts(row: object) -> RecordingFacts:
    """A selected `dive_recording` row as the gates' input.

    Takes any row exposing the columns rather than a model instance, because every query
    here selects explicit columns - the same discipline `_find_by_digest` follows in
    `dive_files.py`, and for the same reason: what a decision reads should be visible in
    the statement that reads it.
    """
    return RecordingFacts(
        device=DeviceIdentity(
            brand=row.device_brand,  # type: ignore[attr-defined]
            model=row.device_model,  # type: ignore[attr-defined]
            serial=row.device_serial,  # type: ignore[attr-defined]
            dive_number=row.device_dive_number,  # type: ignore[attr-defined]
        ),
        start_time=row.start_time,  # type: ignore[attr-defined]
        utc_offset_minutes=row.utc_offset_minutes,  # type: ignore[attr-defined]
        duration=row.duration,  # type: ignore[attr-defined]
        max_depth=row.max_depth,  # type: ignore[attr-defined]
        sampled_span=row.sampled_span,  # type: ignore[attr-defined]
    )


async def load_candidates(
    db: AsyncSession, *, user_id: int, around: datetime, exclude_dive_id: int | None = None
) -> list[RecordingCandidate]:
    """This account's recordings whose stored start is within the candidate window.

    One indexed query on `(user_id, start_time)`, joined to `dive` for the two things a
    form needs to name a match and to `dive_profile` for the sampled span the
    same-recording gate compares. Deleted dives are excluded: a match that attached a file
    to a dive its owner cannot open would be indistinguishable from the file vanishing.

    **The window is compared against the stored column, not against a wall clock.** It has
    to be, because it is an index scan and the wall clock is not a column - which is
    precisely why the window is fourteen hours wider than any gate can reach. The gates
    then compute the right delta per pair.
    """
    stmt = (
        select(
            DiveRecording.id,
            DiveRecording.uuid,
            DiveRecording.dive_id,
            DiveRecording.ordinal,
            DiveRecording.device_brand,
            DiveRecording.device_model,
            DiveRecording.device_serial,
            DiveRecording.device_dive_number,
            DiveRecording.start_time,
            DiveRecording.utc_offset_minutes,
            DiveRecording.duration,
            DiveRecording.max_depth,
            DiveProfile.duration.label("sampled_span"),
            Dive.uuid.label("dive_uuid"),
            Dive.dive_number,
        )
        .join(Dive, Dive.id == DiveRecording.dive_id)
        .outerjoin(DiveProfile, DiveProfile.recording_id == DiveRecording.id)
        .where(
            DiveRecording.user_id == user_id,
            DiveRecording.start_time.is_not(None),
            DiveRecording.start_time >= around - CANDIDATE_WINDOW,
            DiveRecording.start_time <= around + CANDIDATE_WINDOW,
            Dive.is_deleted.is_(False),
        )
        .order_by(DiveRecording.start_time, DiveRecording.id)
    )
    if exclude_dive_id is not None:
        stmt = stmt.where(DiveRecording.dive_id != exclude_dive_id)

    return [
        RecordingCandidate(
            id=row.id,
            uuid=row.uuid,
            dive_id=row.dive_id,
            dive_uuid=row.dive_uuid,
            dive_number=row.dive_number,
            ordinal=row.ordinal,
            facts=_facts(row),
        )
        for row in await db.execute(stmt)
    ]


async def load_recordings_for_dive(db: AsyncSession, *, dive_id: int) -> list[RecordingCandidate]:
    """One dive's recordings in ordinal order, as the gates' shape.

    The attach route's own candidate set: the same-recording test is applied within the
    target dive only, because the route was told which dive the file belongs to.
    """
    stmt = (
        select(
            DiveRecording.id,
            DiveRecording.uuid,
            DiveRecording.dive_id,
            DiveRecording.ordinal,
            DiveRecording.device_brand,
            DiveRecording.device_model,
            DiveRecording.device_serial,
            DiveRecording.device_dive_number,
            DiveRecording.start_time,
            DiveRecording.utc_offset_minutes,
            DiveRecording.duration,
            DiveRecording.max_depth,
            DiveProfile.duration.label("sampled_span"),
            Dive.uuid.label("dive_uuid"),
            Dive.dive_number,
        )
        .join(Dive, Dive.id == DiveRecording.dive_id)
        .outerjoin(DiveProfile, DiveProfile.recording_id == DiveRecording.id)
        .where(DiveRecording.dive_id == dive_id)
        .order_by(DiveRecording.ordinal)
    )
    return [
        RecordingCandidate(
            id=row.id,
            uuid=row.uuid,
            dive_id=row.dive_id,
            dive_uuid=row.dive_uuid,
            dive_number=row.dive_number,
            ordinal=row.ordinal,
            facts=_facts(row),
        )
        for row in await db.execute(stmt)
    ]


async def next_ordinal(db: AsyncSession, *, dive_id: int) -> int:
    """The slot a new recording of this dive takes - one past the last, or 0.

    Read rather than counted, because a deletion that did not renumber would make a count
    collide. `delete_recording` does renumber, so the two agree; reading the maximum is what
    keeps that an invariant rather than a dependency.
    """
    highest = (
        await db.execute(select(func.max(DiveRecording.ordinal)).where(DiveRecording.dive_id == dive_id))
    ).scalar_one_or_none()
    return 0 if highest is None else int(highest) + 1


async def create_recording(
    db: AsyncSession,
    *,
    dive_id: int,
    user_id: int,
    ordinal: int,
    device: ParsedDevice | None = None,
    start_time: datetime | None = None,
    utc_offset_minutes: int | None = None,
    duration: int | None = None,
    max_depth: float | None = None,
) -> int:
    """Insert one recording and return its row id. Does not commit.

    The device's six members are spread from `DEVICE_COLUMNS` rather than named, so a member
    added to `ParsedDevice` is stored without this function being edited.
    """
    values: dict[str, object] = {
        "dive_id": dive_id,
        "user_id": user_id,
        "ordinal": ordinal,
        "start_time": start_time,
        "utc_offset_minutes": utc_offset_minutes,
        "duration": duration,
        "max_depth": max_depth,
        # Spelled out rather than left to `PublicUUIDMixin`'s `default_factory`: that is a
        # dataclass-level default applied when the ORM constructs an instance, and this
        # Core-level INSERT never constructs one.
        "uuid": uuid7(),
        "created_at": datetime.now(UTC),
    }
    for member, column in DEVICE_COLUMNS.items():
        values[column] = None if device is None else getattr(device, member)

    result = await db.execute(insert(DiveRecording).values(**values).returning(DiveRecording.id))
    return int(result.scalar_one())


async def fill_device_fields(db: AsyncSession, *, recording_id: int, device: ParsedDevice | None) -> None:
    """Fill this recording's blank device columns from a later file. **Never overwrites.**

    A second file of one recording contributes what the first did not carry - the JSON's
    serial beside the FIT's model - and takes nothing from it. The rejected alternative is
    "the later file wins", which loses a value a diver corrected between two uploads and is
    the same objection this repository already records against profile-derived pressures.

    Written as a `COALESCE` per column rather than read-then-write: it is one statement, it
    cannot race a concurrent fill into overwriting anything, and it is the rule stated
    exactly once in SQL instead of once in SQL and once in Python.
    """
    if device is None:
        return
    values = {
        column: func.coalesce(getattr(DiveRecording, column), getattr(device, member))
        for member, column in DEVICE_COLUMNS.items()
        if getattr(device, member) is not None
    }
    if not values:
        return
    await db.execute(update(DiveRecording).where(DiveRecording.id == recording_id).values(**values))


async def fill_gate_figures(
    db: AsyncSession, *, recording_id: int, duration: int | None, max_depth: float | None
) -> None:
    """Fill the two match figures where the recording has none. Same fill-only rule."""
    values: dict[str, object] = {}
    if duration is not None:
        values["duration"] = func.coalesce(DiveRecording.duration, duration)
    if max_depth is not None:
        values["max_depth"] = func.coalesce(DiveRecording.max_depth, max_depth)
    if values:
        await db.execute(update(DiveRecording).where(DiveRecording.id == recording_id).values(**values))


async def fill_start(
    db: AsyncSession, *, recording_id: int, start_time: datetime | None, utc_offset_minutes: int | None
) -> None:
    """Fill a NULL start, and a NULL offset from a file that recorded one.

    **The offset fill is a same-recording privilege and no other match's.** A second file of
    the same recording is the same device saying the same thing twice, so an offset one of
    them wrote is a fact about this record rather than an invention. A second *device*'s
    offset is its own clock's and says nothing about this one, which is why the same-dive
    gates never reach this.

    **The two columns are filled together or not at all, and this is the one fill in the
    module that cannot be a pair of `COALESCE`s.** They are not two independent values: a NULL
    offset means `start_time` holds a *wall clock labelled UTC* rather than an instant
    (`core/utils/datetime_offset.py`), so writing an offset onto a row that already has a
    start silently reinterprets a column nobody rewrote - and `combine_start_time` then reads
    the recording back three hours late for exactly the Shearwater pair the *Clocks* rule
    exists for. Filling the offset therefore converts the wall clock to the instant it names,
    which preserves the clock face the diver read and is the only reading under which both
    columns stay true.

    Read-then-write rather than one statement, deliberately: expressing that as SQL means a
    `CASE` over both columns, and the rule is hard enough to state once. Every caller is
    inside a transaction on a dive only its owner can reach, so there is no race for the read
    to lose.
    """
    if start_time is None:
        return

    stored = (
        await db.execute(
            select(DiveRecording.start_time, DiveRecording.utc_offset_minutes).where(DiveRecording.id == recording_id)
        )
    ).one_or_none()
    if stored is None:  # pragma: no cover - the caller matched against this row
        return

    if stored.start_time is None:
        values: dict[str, object] = {"start_time": start_time, "utc_offset_minutes": utc_offset_minutes}
    elif stored.utc_offset_minutes is None and utc_offset_minutes is not None:
        # The stored column is the wall clock; the incoming offset is what makes it an
        # instant. Subtracting rather than trusting the incoming `start_time` keeps the
        # stored recording's own clock reading, which may differ from this file's by the two
        # seconds the same-recording gate admits.
        values = {
            "start_time": stored.start_time - timedelta(minutes=utc_offset_minutes),
            "utc_offset_minutes": utc_offset_minutes,
        }
    else:
        # A start already known on the terms it was written under. Nothing to fill, and
        # nothing here may overwrite.
        return

    await db.execute(update(DiveRecording).where(DiveRecording.id == recording_id).values(**values))


async def storage_keys_for_recordings(db: AsyncSession, *, recording_ids: Sequence[int]) -> list[str]:
    """Every stored file key these recordings hold.

    Collected *before* the delete, because the row cascade takes the `dive_file` rows with
    it and `blob_store.delete_after_commit` needs the keys after that has happened. The
    ordering rule is the module-wide one: delete the row, then unlink after the commit.
    """
    if not recording_ids:
        return []
    rows = await db.execute(select(DiveFile.storage_key).where(DiveFile.recording_id.in_(set(recording_ids))))
    return list(rows.scalars())


async def delete_recording(db: AsyncSession, *, recording_id: int, dive_id: int, commit: bool = False) -> None:
    """Remove one recording, its files and its profile, then close the ordinal gap.

    The files and the profile go by the FK cascade rather than by hand - unlike the dive
    cascade, this one really fires, because recordings are hard-deleted. The blobs still
    need unlinking after the commit, which is why the keys are read first.
    """
    keys = await storage_keys_for_recordings(db, recording_ids=[recording_id])
    await db.execute(delete(DiveRecording).where(DiveRecording.id == recording_id))
    blob_store.delete_after_commit(db, keys)
    await renumber_ordinals(db, dive_id=dive_id)
    if commit:
        await db.commit()


async def renumber_ordinals(db: AsyncSession, *, dive_id: int) -> None:
    """Close any gap in a dive's ordinals, keeping the order. 0 is primary afterwards.

    **Every row moves through a temporary negative slot first.** `ux_dive_recording_dive_id_
    ordinal` is checked per statement, so shifting 1 -> 0 while 0 still exists is a
    violation even when the row at 0 is about to move too; negating first frees the whole
    range and cannot itself collide, since no stored ordinal is ever negative.
    """
    rows = (
        (
            await db.execute(
                select(DiveRecording.id).where(DiveRecording.dive_id == dive_id).order_by(DiveRecording.ordinal)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return

    await db.execute(
        update(DiveRecording).where(DiveRecording.dive_id == dive_id).values(ordinal=-DiveRecording.ordinal - 1)
    )
    for position, row_id in enumerate(rows):
        await db.execute(update(DiveRecording).where(DiveRecording.id == row_id).values(ordinal=position))


async def make_primary(db: AsyncSession, *, recording_id: int, dive_id: int) -> None:
    """Move one recording to ordinal 0, keeping the rest in their existing order.

    Through the same negative-slot pass as `renumber_ordinals`, and for the same reason.
    """
    rows = (
        (
            await db.execute(
                select(DiveRecording.id).where(DiveRecording.dive_id == dive_id).order_by(DiveRecording.ordinal)
            )
        )
        .scalars()
        .all()
    )
    if recording_id not in rows:
        raise RecordingNotFoundError("That recording does not belong to this dive.")

    ordered = [recording_id, *(row_id for row_id in rows if row_id != recording_id)]
    await db.execute(
        update(DiveRecording).where(DiveRecording.dive_id == dive_id).values(ordinal=-DiveRecording.ordinal - 1)
    )
    for position, row_id in enumerate(ordered):
        await db.execute(update(DiveRecording).where(DiveRecording.id == row_id).values(ordinal=position))


async def primary_recording_ids(db: AsyncSession, *, dive_ids: Sequence[int]) -> dict[int, int]:
    """Each dive's ordinal-0 recording id, for the surfaces that take one profile.

    The app's own UDDF export and the tech scalars are both "the primary recording's", which
    is the reference writer's rule and this app's: a document with one profile per dive
    carries the one a reader would show by default rather than an arbitrary one.
    """
    if not dive_ids:
        return {}
    rows = await db.execute(
        select(DiveRecording.dive_id, DiveRecording.id).where(
            DiveRecording.dive_id.in_(set(dive_ids)), DiveRecording.ordinal == 0
        )
    )
    return {row.dive_id: row.id for row in rows}


async def resolve_recording(db: AsyncSession, *, dive_id: int, uuid: uuid_pkg.UUID) -> int:
    """One of this dive's recordings by public uuid, or `RecordingNotFoundError`.

    Scoped to the dive rather than looked up globally, so a uuid belonging to another dive -
    the caller's own or anybody's - is indistinguishable from one that does not exist. The
    same reasoning as `fetch_owned_or_raise`'s 404-not-403.
    """
    row_id = (
        await db.execute(select(DiveRecording.id).where(DiveRecording.dive_id == dive_id, DiveRecording.uuid == uuid))
    ).scalar_one_or_none()
    if row_id is None:
        raise RecordingNotFoundError("This dive has no such recording.")
    return int(row_id)


async def get_recordings_for_dives(db: AsyncSession, *, dive_ids: Sequence[int]) -> dict[int, list[RecordingRead]]:
    """Every dive's recordings, with their files and profile summaries, in three queries.

    Batched though only the detail endpoint asks for it, and only ever for one dive - the
    `get_file_infos_for_dives` shape, for the reason that one gives: it makes the
    explicit-columns discipline the default, and it is what a recordings marker in the dive
    list would need without a rewrite.
    """
    if not dive_ids:
        return {}

    rows = (
        (
            await db.execute(
                select(DiveRecording)
                .where(DiveRecording.dive_id.in_(set(dive_ids)))
                .order_by(DiveRecording.dive_id, DiveRecording.ordinal)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return {dive_id: [] for dive_id in dive_ids}

    recording_ids = [row.id for row in rows]
    profiles = await get_profile_infos_for_recordings(db, recording_ids=recording_ids)
    files = await get_file_infos_for_recordings(db, recording_ids=recording_ids)

    by_dive: dict[int, list[RecordingRead]] = {dive_id: [] for dive_id in dive_ids}
    for row in rows:
        by_dive.setdefault(row.dive_id, []).append(
            RecordingRead(
                uuid=row.uuid,
                ordinal=row.ordinal,
                device=_read_device(row),
                started_at=(
                    None if row.start_time is None else combine_start_time(row.start_time, row.utc_offset_minutes)
                ),
                files=files.get(row.id, []),
                profile=profiles.get(row.id),
                updated_at=row.updated_at,
            )
        )
    return by_dive


def _read_device(row: DiveRecording) -> RecordingDevice | None:
    """The six columns as the response's device, or `None` when the file named nothing.

    `None` rather than an object of six nulls, on `ParsedDiveSchema._drop_empty_device`'s
    terms: a recording whose source said nothing about the computer must not come back
    claiming it named one.
    """
    members = {member: getattr(row, column) for member, column in DEVICE_COLUMNS.items()}
    if all(value is None for value in members.values()):
        return None
    return RecordingDevice(**members)


async def get_file_infos_for_recordings(
    db: AsyncSession, *, recording_ids: Sequence[int]
) -> dict[int, list[DiveFileInfo]]:
    """Each recording's stored files, in attach order.

    `id` orders them, which is attach order by construction - a later upload gets a higher
    sequence value - and it is what the fill rule means by "the first file that recorded
    it".
    """
    if not recording_ids:
        return {}
    stmt = (
        select(
            DiveFile.recording_id,
            DiveFile.uuid,
            DiveFile.content_type,
            DiveFile.byte_size,
            DiveFile.original_filename,
            DiveFile.parser_key,
            DiveFile.updated_at,
        )
        .where(DiveFile.recording_id.in_(set(recording_ids)))
        .order_by(DiveFile.recording_id, DiveFile.id)
    )
    infos: dict[int, list[DiveFileInfo]] = {}
    for row in await db.execute(stmt):
        infos.setdefault(row.recording_id, []).append(
            DiveFileInfo(
                uuid=row.uuid,
                content_type=row.content_type,
                byte_size=row.byte_size,
                original_filename=row.original_filename,
                parser_key=row.parser_key,
                updated_at=row.updated_at,
            )
        )
    return infos
