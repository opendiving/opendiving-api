"""Storage for the dive-computer exports a dive's recordings were read from.

The **only** module that knows a recording has stored bytes at all. Routes go through these
functions and never see where the bytes are, which is what let the payload move out of a
`bytea` column and onto the files volume without a single call site changing - the same
seam, for the same reasons, as `services/certification_files.py`.
`services/blob_store.py` is the layer below, and the only one that knows where the bytes
actually are - a filesystem volume or an S3-compatible bucket, on `FILE_STORAGE_BACKEND`.

**A file belongs to a recording, and a recording may hold several.** Which recording an
incoming file lands in is `services/dive_recordings.py`'s decision; what this module owns is
what happens once it has been made - the bytes, the row, the fill rule across a recording's
files, and what is derived from them (the profile, the recording's readouts, and the dive's
entry and exit fixes).

*A second file of one recording fills and never overwrites*, and that rule is written once per
thing it applies to - the device columns, the two match figures, the recording's start, its
readouts, the dive's tech scalars, the profile's channels and the dive's cylinders. Derive them from
`git grep -n "def fill_" -- src/app/services` rather than from a count here, which is what
stops the list going stale; read it as a superset, since the cylinder one is implemented by
two pure helpers that match the same grep and are not themselves things the rule applies to.
Each takes every value from the *first* file that recorded it. The rejected alternative is
"the later file wins", which silently loses a value a diver corrected between two uploads.
"""

import hashlib
import logging
import uuid as uuid_pkg
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Literal

from fastapi import UploadFile
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from uuid6 import uuid7

from ..core.db.database import release_read_transaction
from ..core.security import verify_dive_file_token
from ..core.utils.uploads import read_upload_within_limit, safe_filename
from ..models.dive import Dive
from ..models.dive_file import DiveFile
from ..models.dive_mixture import DiveMixture
from ..models.dive_recording import DiveRecording
from ..schemas.dive import DiveFileInfo, DiveTechScalars
from ..schemas.dive_mixture import DiveMixtureCreate, DiveMixtureRead, as_create
from ..schemas.dive_profile import MILLISECONDS_PER_SECOND
from ..schemas.parsed_dive import DiveMixtureSchema, ParsedDiveSchema
from . import blob_store, dive_recordings
from .dive_profiles import (
    READER_VERSION,
    NormalizedProfile,
    attribute_and_cap,
    delete_profile_for_recording,
    delete_profiles_for_dive,
    fill_channels,
    get_existing_profile,
    load_stored_profile,
    recording_source_digest,
    replace_profile_samples,
    shift_profile,
    should_extract,
    store_profile,
)
from .dive_reader import (
    DiveFileReadError,
    ReadDive,
    UnsupportedDiveFileError,
    content_type_of,
    prefill,
    read_dive_file,
    reads,
    shape,
    start_of,
)
from .recording_shape import gate_figures
from .storage_usage import ensure_room, storage_limit_bytes

logger = logging.getLogger(__name__)

# The key prefix every stored export is written under - see `blob_store.new_key`.
KEY_KIND = "dive-files"

# Matches the cap `/dive/parse` reads under, since the same file makes both trips: a
# limit here that was lower would let a file pre-fill a form and then be refused
# storage. Exports are small (a few hundred KB); this is headroom, not a target.
MAX_DIVE_FILE_SIZE = 5 * 1024 * 1024  # 5 MB

# The `dive` columns an import owns outright - the primary recording's entry and exit fixes.
# Taken from `DiveTechScalars` rather than listed here, so the schema that publishes them and
# the write that fills them cannot drift: adding a field to one is adding it to both.
TECH_SCALAR_FIELDS = tuple(DiveTechScalars.model_fields)
# And the recording's, which every recording's own files write.
READOUT_FIELDS = dive_recordings.READOUT_COLUMNS
# Everything a file's header yields as a plain number, both halves - what `FileExtraction`
# carries before the write splits it between the recording and the dive.
SCALAR_FIELDS = (*READOUT_FIELDS, *TECH_SCALAR_FIELDS)


class InvalidDiveFileTokenError(Exception):
    """The upload wasn't accompanied by proof that this server parsed these bytes."""


class DiveFileAlreadyLinkedError(Exception):
    """These exact bytes are already stored as another dive's source file."""

    def __init__(self, dive_uuid: uuid_pkg.UUID | None) -> None:
        self.dive_uuid = dive_uuid
        super().__init__("This file is already attached to another dive.")


class DiveFileConflictError(Exception):
    """A concurrent upload won a race on one of the table's unique indexes."""


class DiveFileNotFoundError(Exception):
    """No file of this dive under that uuid."""


@dataclass(frozen=True, slots=True)
class LoadedDiveFile:
    """A stored export's bytes plus everything a reader of them needs.

    `sha256` and `parser_key` ride along because the re-extraction paths need both: a
    recording's profile is a function of its files' digests in order, and a file is re-read
    as the format id it was admitted under rather than sniffed again.
    """

    data: bytes
    content_type: str
    original_filename: str
    sha256: str
    parser_key: str = ""


@dataclass(frozen=True, slots=True)
class _ExistingRow:
    """The dedupe lookup's result - metadata only, never the payload."""

    id: int
    dive_id: int
    recording_id: int
    uuid: uuid_pkg.UUID
    content_type: str
    byte_size: int
    original_filename: str
    parser_key: str
    storage_key: str
    updated_at: datetime | None


def reconcile(existing: _ExistingRow | None, dive_id: int) -> Literal["noop", "conflict", "insert"]:
    """Decide what an upload of already-hashed bytes should do.

    Split out from `store_recording_file` so the decision is testable without a database.
    There are still only three outcomes, and that is a consequence of `dive_id` being NOT
    NULL: every stored row belongs to a dive the diver can still reach, so there is no
    fourth "orphaned row you could re-claim" case to handle.

    - `noop`: these exact bytes are already stored against this dive. The attach is
      idempotent, and the recording they are in is what comes back.
    - `conflict`: the same bytes are some *other* dive's. Reported rather than resolved -
      see `store_recording_file`.
    - `insert`: unseen bytes.

    **The comparison is on the dive and not on the recording**, which recordings did not
    change: `ux_dive_file_user_id_sha256` is per diver, so one set of bytes is one row and
    that row is in exactly one recording of exactly one dive. Asking about the recording
    instead would answer "insert" for bytes already stored against another recording of this
    same dive, and the insert would then die on that index.
    """
    if existing is None:
        return "insert"
    if existing.dive_id == dive_id:
        return "noop"
    return "conflict"


def _info(row: _ExistingRow) -> DiveFileInfo:
    return DiveFileInfo(
        uuid=row.uuid,
        content_type=row.content_type,
        byte_size=row.byte_size,
        original_filename=row.original_filename,
        parser_key=row.parser_key,
        updated_at=row.updated_at,
    )


async def _find_by_digest(db: AsyncSession, *, user_id: int, digest: str) -> _ExistingRow | None:
    """Look up this diver's row for a given content hash.

    Explicit columns rather than `select(DiveFile)`. That predates the payload leaving
    Postgres - it kept the `bytea` from being dragged along by accident - and stays because
    it says exactly what the dedupe decision reads.
    """
    stmt = select(
        DiveFile.id,
        DiveFile.dive_id,
        DiveFile.recording_id,
        DiveFile.uuid,
        DiveFile.content_type,
        DiveFile.byte_size,
        DiveFile.original_filename,
        DiveFile.parser_key,
        DiveFile.storage_key,
        DiveFile.updated_at,
    ).where(DiveFile.user_id == user_id, DiveFile.sha256 == digest)
    row = (await db.execute(stmt)).one_or_none()
    return None if row is None else _ExistingRow(*row)


async def _ensure_room_for(db: AsyncSession, *, user_id: int, data: bytes) -> None:
    await ensure_room(
        db,
        user_id=user_id,
        incoming=blob_store.stored_size_ceiling(KEY_KIND, len(data)),
        exact=lambda: blob_store.stored_size(KEY_KIND, data),
    )


async def ensure_room_to_attach(db: AsyncSession, *, user_id: int, data: bytes, digest: str) -> None:
    """Refuse an export this account would have no room to store, before anything parses it.

    `POST /dive/parse` asks this of every upload, so a diver learns before filling in the
    form rather than after. Bytes the account already holds add nothing and pass: a repeat
    attach is the no-op `_repeat_upload` makes it, and the repair after a lost blob re-puts
    under the row's own key - and attach admits only bytes carrying a parse's token, so a
    refusal here would make both impossible.
    """
    if storage_limit_bytes() is None or await _find_by_digest(db, user_id=user_id, digest=digest) is not None:
        return
    await _ensure_room_for(db, user_id=user_id, data=data)


@dataclass(frozen=True, slots=True)
class FileExtraction:
    """Everything one file yields, read through the one reader.

    `parsed` is the dive form's projection of the file (`dive_reader.prefill`) - its device,
    settings, cylinders, fixes and start - and is `None` when the file could not be read at
    all, which is a different fact from a file that read and said nothing: the first leaves
    the recording's device columns untouched, the second is a device the file did not name.

    **`profile` is shaped and nothing more**, exactly as logbook import shapes a document's
    recording (`recording_shape.shape_profile`) - on the file's own axis, counted from `start`,
    and not attributed, capped, relabelled or placed on the recording's clock. Those steps
    belong to the recording rather than to the file: `extract_recording` shifts each file onto
    the recording's stored start, maps its cylinder labels onto the recording's, fills the
    channels across them and then attributes and caps *once*, which is the only order under
    which the gas attribution is computed against the channels it will be stored beside and
    at the resolution `derive_gas_attribution` requires.

    `scalars` are the recording's readouts as the import stores them, beside the dive's
    entry and exit fixes as the form shows them.
    """

    parsed: ParsedDiveSchema | None
    profile: NormalizedProfile | None
    scalars: dict[str, float | None] | None
    start: tuple[datetime, int | None] | None = None


_UNREAD = FileExtraction(parsed=None, profile=None, scalars=None)


def extract_file(content: bytes, format: str) -> FileExtraction:
    """One file read as the format it was admitted under, never raising.

    **Never raises**, because the file is the durable artifact: a file this build can no
    longer read must not fail the upload that would have preserved it, nor a backfill run
    over a corpus. It comes back empty, which `extract_recording` counts as unreadable - a
    reader regression to report, not a file that said nothing.
    """
    try:
        read = read_dive_file(content, format=format)
    except UnsupportedDiveFileError, DiveFileReadError:
        logger.warning("A stored %s file could not be read", format, exc_info=True)
        return _UNREAD
    except Exception:
        logger.exception("Unexpected error reading a %s file", format)
        return _UNREAD
    return extraction_of(read)


def extraction_of(read: ReadDive) -> FileExtraction:
    """What one file read as one dive yields, from the read rather than the bytes.

    Split from `extract_file` for logbook import, which has already converted the file to
    plan it and hands that conversion here rather than converting it twice.
    """
    shaped = shape(read)
    parsed = prefill(read, shaped)
    readouts = {} if shaped is None else shaped.readouts
    return FileExtraction(
        parsed=parsed,
        profile=None if shaped is None else shaped.profile,
        scalars={
            **{name: readouts.get(name) for name in READOUT_FIELDS},
            **{name: getattr(parsed, name) for name in TECH_SCALAR_FIELDS},
        },
        start=start_of(read, shaped),
    )


@dataclass(frozen=True, slots=True)
class RecordingExtraction:
    """What a recording's files say, read in attach order under the fill rule.

    One object rather than several returns because they are read together everywhere and every
    one of them is *the first file that recorded it* - only the unit differs. The profile
    takes each channel whole from the earliest file carrying it; the scalars take each
    reading likewise; the device likewise; and the cylinders take each *member* of each
    cylinder likewise, which is one level finer because a Suunto's two exports of one dive
    split the pressures and the gas fraction between them. `unreadable` says at least one
    file could not be re-read at all, which is a reader regression the backfill counts rather
    than swallows.
    """

    profile: NormalizedProfile | None = None
    scalars: dict[str, float | None] = field(default_factory=dict)
    device_source: ParsedDiveSchema | None = None
    mixtures: list[DiveMixtureSchema] = field(default_factory=list)
    unreadable: bool = False


def _placed(
    profile: NormalizedProfile | None,
    file_start: tuple[datetime, int | None] | None,
    origin: tuple[datetime, int | None] | None,
) -> NormalizedProfile | None:
    """One file's profile moved onto the recording's axis: by its start's signed offset from the origin.

    Measured on `delta_seconds`' clock - between instants when both carry an offset, between
    wall clocks when either does not - since a file's start and a recording's imported one may
    be spelled one each way. A file that states no start, or a recording with no origin to
    measure against, is left where it is.
    """
    if profile is None or file_start is None or origin is None:
        return profile
    offset = dive_recordings.signed_delta_seconds(*file_start, *origin)
    return shift_profile(profile, round(offset * MILLISECONDS_PER_SECOND))


def extract_recording(
    files: Sequence[LoadedDiveFile],
    known: Mapping[str, FileExtraction] | None = None,
    *,
    start_time: datetime | None,
    utc_offset_minutes: int | None,
) -> RecordingExtraction:
    """Read a recording's files in order and fill each answer from the first that carries it.

    Pure and DB-free, on this module's `reconcile` idiom: the fill rule is the decision worth
    testing, and it is testable with a list of bytes and no database at all. **Pure CPU, and
    the whole of it** - every caller on a request path hands it to `run_in_threadpool`, for
    the reason *"Uploaded files are parsed in a thread, not on the event loop"* in
    `DECISIONS.md` gives.

    **`start_time` and `utc_offset_minutes` are the recording's stored start**, the
    `started_at` the export writes and the instant its profile's axis counts from. Each file's
    profile is shifted by its own start's offset from it - the first file's included - before
    a channel is taken from it, so a file whose clock differs from the recording's lands where
    its samples happened. A recording with no stored start takes the first stated one.

    **A later file's cylinder labels are mapped onto the recording's before its channels
    join**, `join_file_mixtures` deciding the map, so a pressure channel names the recording's
    cylinder whichever of two files arrived first - the FIT of a pair labels nothing, its
    JSON labels the one cylinder that carried a transmitter, and the pair has to come out the
    same either way round.

    **Order is attach order** and the caller guarantees it (`ORDER BY dive_file.id`), because
    "the first file that recorded it" is meaningless without one. A file recorded under a
    format this build no longer reads, or which stopped reading, sets `unreadable` and
    contributes nothing - it is not silently treated as a file that said nothing, because
    those two facts lead to opposite repairs.

    `known` maps a digest to an extraction the caller already has, and exists for exactly one
    caller: the attach path has just read the incoming file to decide which recording it
    belongs to, and would otherwise read it a second time here.
    """
    result = RecordingExtraction()
    origin = None if start_time is None else (start_time, utc_offset_minutes)
    for file in files:
        extraction = (known or {}).get(file.sha256)
        if extraction is None:
            if not reads(file.parser_key):
                logger.warning("A stored file is recorded under a format this build does not read: %r", file.parser_key)
                result = replace(result, unreadable=True)
                continue
            extraction = extract_file(file.data, file.parser_key)
        if extraction.parsed is None and extraction.profile is None and extraction.scalars is None:
            result = replace(result, unreadable=True)
            continue

        origin = origin or extraction.start
        mixtures, labels = join_file_mixtures(
            result.mixtures, [] if extraction.parsed is None else extraction.parsed.mixtures
        )
        placed = apply_gas_mapping(_placed(extraction.profile, extraction.start, origin), labels)
        result = RecordingExtraction(
            profile=fill_channels(result.profile, placed),
            # `|` with the stored side second is the fill: a key already carrying a value
            # keeps it, and one carrying `None` is overwritten by a later file's reading.
            scalars={
                name: result.scalars.get(name) if result.scalars.get(name) is not None else value
                for name, value in (extraction.scalars or {}).items()
            }
            or result.scalars,
            device_source=result.device_source or extraction.parsed,
            mixtures=mixtures,
            unreadable=result.unreadable,
        )

    # Attributed and capped **once, here**, over the filled channels - which is why every
    # `FileExtraction.profile` above is shaped and nothing more. Attribution reads the gas
    # switches back against the depth channel and after a fill those two may have come from
    # different files, so it has to be derived from the merged result; and it has to be derived
    # *before* the cap, because `downsample` keeps each bucket's extremes and throws the rest
    # away.
    return replace(result, profile=attribute_and_cap(result.profile))


async def store_tech_scalars(
    db: AsyncSession, *, dive_id: int, scalars: dict[str, float | None], commit: bool = False
) -> None:
    """Write a dive's parsed entry and exit fixes **outright**, in the caller's transaction.

    Outright means every field of `DiveTechScalars`, `None` included - so a re-derivation
    that no longer yields a reading clears the one that is there rather than stranding a
    number nothing can re-derive. Used where the recording's whole set of files has just
    been read and the dive had nothing on it to lose: the file that *created* a primary
    recording, and every re-derivation after a deletion or a promotion. Not a primary
    recording's first file as such - see `rederive_recording` on what `fresh` means.

    `commit=False` by default for the same reason as `store_profile`: the attach path writes
    the file, the profile and these in one transaction, so a dive can never end up
    describing a recording it does not have.
    """
    await db.execute(
        update(Dive).where(Dive.id == dive_id).values(**{name: scalars.get(name) for name in TECH_SCALAR_FIELDS})
    )
    if commit:
        await db.commit()


async def fill_tech_scalars(db: AsyncSession, *, dive_id: int, scalars: dict[str, float | None]) -> None:
    """Write only the fixes the dive does not already have. **Never overwrites.**

    The tech-scalar half of *a second file of one recording fills and never overwrites*: a
    FIT arriving beside a JSON of one dive leaves the positions the JSON supplied exactly as
    they are.

    A `COALESCE` per column rather than read-then-write, for `fill_device_fields`' reason:
    one statement, no read to race, and the rule stated once in SQL instead of once in SQL
    and once in Python.
    """
    values = {
        name: func.coalesce(getattr(Dive, name), value)
        for name, value in scalars.items()
        if name in TECH_SCALAR_FIELDS and value is not None
    }
    if values:
        await db.execute(update(Dive).where(Dive.id == dive_id).values(**values))


# A cylinder arriving from a file (`DiveMixtureSchema`) or from another dive's stored rows
# (`DiveMixtureRead`, the other half of a merge). The pairing and the fill read only members
# both shapes carry and mean the same thing by.
IncomingCylinder = DiveMixtureSchema | DiveMixtureRead


@dataclass(frozen=True, slots=True)
class CylinderPair:
    """One of the dive's rows and the incoming cylinder the labelling paired it with.

    `settled` is a pair no later arrival can re-pair: one made by mix, or the only row left
    unpaired after the mix pass meeting the only cylinder left. Any other pair made by
    position is a guess, and a later file bringing the mix can pair that cylinder elsewhere.
    """

    row: DiveMixtureRead
    incoming: IncomingCylinder
    settled: bool


def pair_cylinders(
    parsed: Sequence[IncomingCylinder], stored: Sequence[DiveMixtureRead]
) -> tuple[list[CylinderPair], list[IncomingCylinder]]:
    """The dive's rows paired with a recording's cylinders, and the cylinders nothing paired.

    **By mix first, then by order**, for both labellings - `relabel_gas_numbers` for a
    recording past the first and `renumber_onto_labels` for the primary - and for the fill
    each makes at an arrival, so a pressure channel's cylinder and the row its values land in
    are one row by construction. The mix is the part of a cylinder a diver has no reason to
    retype and every reason to leave alone, so two rows agreeing on `(oxygen, helium)` are the
    same tank; position is the weaker fallback, kept because a pair of air cylinders records
    no distinguishing mix at all. `stored` must be in saved order, which
    `get_mixtures_for_dive`'s `ORDER BY id` guarantees and nothing here can check.
    """
    remaining = list(stored)
    pairs: list[CylinderPair] = []
    by_position: list[IncomingCylinder] = []
    for incoming in parsed:
        match = next(
            (
                row
                for row in remaining
                if row.oxygen is not None
                and incoming.oxygen is not None
                and row.oxygen == incoming.oxygen
                and row.helium == incoming.helium
            ),
            None,
        )
        if match is None:
            by_position.append(incoming)
            continue
        remaining.remove(match)
        pairs.append(CylinderPair(row=match, incoming=incoming, settled=True))
    last = len(remaining) == 1 and len(by_position) == 1
    unmatched: list[IncomingCylinder] = []
    for incoming in by_position:
        if remaining:
            pairs.append(CylinderPair(row=remaining.pop(0), incoming=incoming, settled=last))
        else:
            unmatched.append(incoming)
    return pairs, unmatched


# What a pair may put into the dive's row where the row has none. `po2_limit`, `role`,
# `gas_number` and `usage` are deliberately not here, and each for its own reason: `usage` is
# a distinction no format records at all, `po2_limit` and `role` are the diver's plan rather
# than the tank's contents, and `gas_number` is the join key to the profile's pressure
# channels, which only the labelling writes.
FILLABLE_MIXTURE_FIELDS = ("oxygen", "helium", "volume", "start_pressure", "end_pressure")

# Every member of a stored row, the label aside - what a guessed pair has to find blank, or
# recorded with the incoming cylinder's own value, before it may fill.
_RECORDED_MEMBERS = (*FILLABLE_MIXTURE_FIELDS, "po2_limit", "role", "usage")


def fill_from_pair(pair: CylinderPair, *, trust_position: bool) -> dict[str, float]:
    """What one pair's incoming cylinder puts into the dive's row. **Never overwrites.**

    **A dive's cylinders are the dive's**, so a blank one takes the value from whichever
    recording pairs with it: the list is the one the diver maintains, shared by every
    recording, and the labelling already asserts the pairing by pointing that recording's
    pressure channel at the row. A blank credits no one.

    - The mix and the volume fill per member, each only where the row has none.
    - **The pressures fill as a pair**: only into a row carrying neither, and only from a
      cylinder carrying a start. A start from one source beside an end from another is a
      drain nobody measured, which `compute_gas_use` would read as gas breathed - and a row
      stored with an end of 0 and no start is a row carrying one.
    - A pair whose recorded fractions disagree fills nothing: it is a positional pair
      describing another tank.
    - A row the fill would leave outside the table's constraints is left whole
      (`_fill_is_storable`).

    **`trust_position` is the primary recording's, whose rows came from its own file.** For
    any other recording, and for the merge's absorbed rows, a pair made by position is a
    guess (`CylinderPair.settled`), and it fills only where the row records nothing the
    incoming cylinder does not record with the same value, the label aside: a watch whose
    tank pressures arrive before the file naming their mixes would otherwise put the deco
    bottle's drain on the back gas's row. A row this same recording filled at an earlier
    arrival still takes the rest, since the recording's re-derivation carries what it wrote.
    """
    row, incoming = pair.row, pair.incoming
    if not (trust_position or pair.settled or _records_nothing_else(row, incoming)):
        return {}
    if _mixes_disagree(row, incoming):
        return {}
    values: dict[str, float] = {
        name: reading
        for name in ("oxygen", "helium", "volume")
        if getattr(row, name) is None and (reading := getattr(incoming, name)) is not None
    }
    if row.start_pressure is None and row.end_pressure is None and incoming.start_pressure is not None:
        values["start_pressure"] = incoming.start_pressure
        if incoming.end_pressure is not None:
            values["end_pressure"] = incoming.end_pressure
    return values if _fill_is_storable(row, values) else {}


def _records_nothing_else(row: DiveMixtureRead, incoming: IncomingCylinder) -> bool:
    """Whether every member the row records, the label aside, the incoming cylinder records too."""
    return all(
        getattr(row, name) is None or getattr(row, name) == getattr(incoming, name, None) for name in _RECORDED_MEMBERS
    )


def _carries_nothing(cylinder: IncomingCylinder) -> bool:
    """A cylinder with no member at all, its label included."""
    return all(value is None for name, value in cylinder if name != "id")


def relabel_gas_numbers(
    parsed: Sequence[IncomingCylinder], stored: Sequence[DiveMixtureRead], *, fill: bool
) -> tuple[dict[int, int], list[DiveMixtureCreate] | None]:
    """Map a second computer's cylinder labels onto the dive's own list, and at an arrival fill
    the blanks of the rows it pairs with.

    Returns `(old gas_number -> the dive's gas_number, the dive's cylinders to write)`, the
    second `None` where the dive's rows stand as they are.

    **Either shape comes in, and a cylinder carried across keeps what it had.** A *file's*
    cylinders come from the attach and import paths, a *dive's* stored ones from the other
    half of a merge. A stored row goes through `as_create`, which keeps `usage` - a member no
    format records and only a diver can have typed, which is exactly the value that must
    survive the cylinder being carried onto another dive.

    **`gas_number` is dive-scoped, and that is the ruling this implements.** The app derives
    gas consumption from the diver's editable cylinders joined to a profile's pressure
    channels and gas-switch events by that label (see *"The cylinder pressures come from the
    mixtures"* in `DECISIONS.md`), so a second computer whose own labelling calls the deco
    bottle `1` would otherwise attribute its pressures to the dive's back gas. Subsurface
    renumbers a second computer's sensors onto the dive's cylinder list for the same reason.

    **Paired by `pair_cylinders`, and only then appended.** A cylinder the dive's list does
    not have is a real one the second computer saw, appended with the next free label rather
    than dropped - **unless it carries nothing at all**: a Shearwater Cloud UDDF lists six
    tank slots of 0 bar linked to no mix, and appending them would put blank rows on every
    dive that computer joins. It still takes its place in the positional pairing, filling
    nothing where it pairs. **A matched row with no label takes the next free one** where the
    incoming cylinder has a label to map: the reader labels only a cylinder a channel points
    at, so a dive logged from one FIT has none, and the second computer's channel would
    otherwise name a label no cylinder of the dive carries.

    **`fill` says this is an arrival** - `rederive_recording`'s rule - and each pair then
    fills on `fill_from_pair`'s terms, a guessed pair bounded. Not on every re-derivation:
    a value the diver cleared would come back on the next backfill.
    """
    pairs, unmatched = pair_cylinders(parsed, stored)

    next_free = max((row.gas_number for row in stored if row.gas_number is not None), default=0) + 1
    mapping: dict[int, int] = {}
    changes: dict[int, dict[str, float | int]] = {}
    for pair in pairs:
        row, incoming = pair.row, pair.incoming
        change: dict[str, float | int] = {**(fill_from_pair(pair, trust_position=False) if fill else {})}
        if incoming.gas_number is not None:
            label = row.gas_number
            if label is None:
                label = change["gas_number"] = next_free
                next_free += 1
            mapping[incoming.gas_number] = label
        if change:
            changes[row.id] = change

    appended: list[DiveMixtureCreate] = []
    for incoming in unmatched:
        if _carries_nothing(incoming):
            continue
        if incoming.gas_number is not None:
            mapping[incoming.gas_number] = next_free
        cylinder = (
            as_create(incoming) if isinstance(incoming, DiveMixtureRead) else DiveMixtureCreate(**incoming.model_dump())
        )
        appended.append(cylinder.model_copy(update={"gas_number": next_free}))
        next_free += 1

    if not changes and not appended:
        return mapping, None
    return mapping, [*(as_create(row.model_copy(update=changes.get(row.id, {}))) for row in stored), *appended]


def apply_gas_mapping(profile: NormalizedProfile | None, mapping: dict[int, int]) -> NormalizedProfile | None:
    """Rewrite a profile's cylinder labels through `relabel_gas_numbers`' map.

    Every place a `gas_number` appears on the stored row - the pressure channels and the
    `gas_switch` events in the payload, and the `gas_attribution` summary beside it - because
    a map applied to one and not the others would leave a dive whose switches, curves and
    time on gas name different tanks. The attribution is mapped rather than re-derived: it was
    derived from the full-resolution depth channel before `downsample`, and this may be
    handed a profile that has already been thinned.

    A label the map does not mention is left alone. That is the identity case (a label
    already naming the cylinder it should) and it is also the honest answer for a channel
    whose number the mapping could not place.
    """
    if profile is None or not mapping:
        return profile
    return replace(
        profile,
        pressure=[
            replace(series, gas_number=mapping.get(series.gas_number, series.gas_number)) for series in profile.pressure
        ],
        events=[
            replace(event, gas_number=mapping.get(event.gas_number, event.gas_number))
            if event.gas_number is not None
            else event
            for event in profile.events
        ],
        gas_attribution=[
            entry.model_copy(update={"gas_number": mapping.get(entry.gas_number, entry.gas_number)})
            for entry in profile.gas_attribution
        ],
    )


def _fill_is_storable(stored_mix: IncomingCylinder, values: Mapping[str, float]) -> bool:
    """Would the row `values` produces still satisfy `dive_mixture`'s own constraints?

    The table's `CHECK`s over these five columns, restated here because a fill is the one
    write path that can compose a row out of two sources: everything else that reaches these
    columns arrives as a whole cylinder from one place, already validated by
    `DiveMixtureCreate` or by the parse-side bounds on `DiveMixtureSchema`. A fill mixes them,
    and neither of those layers sees the combination. Where the combination fails, the file
    and the stored row cannot both be describing this cylinder, and the stored row stands.

    Deliberately checked rather than caught: `blob_store` writes and row inserts sit in the
    same transaction as this update, and `CHECK` is not deferrable in Postgres, so the
    exception would arrive from the `execute` in the middle of an attach or an import rather
    than at a point either could recover at.
    """
    row = {name: values.get(name, getattr(stored_mix, name)) for name in FILLABLE_MIXTURE_FIELDS}
    oxygen, helium = row["oxygen"], row["helium"]
    start, end = row["start_pressure"], row["end_pressure"]
    volume = row["volume"]
    return not (
        (volume is not None and volume <= 0)
        or (oxygen is not None and not 0 <= oxygen <= 100)
        or (helium is not None and not 0 <= helium <= 100)
        or (oxygen is not None and helium is not None and oxygen + helium > 100)
        or (start is not None and not 0 < start <= 350)
        or (end is not None and not 0 <= end <= 350)
        or (start is not None and end is not None and end > start)
    )


def join_file_mixtures(
    earlier: Sequence[DiveMixtureSchema], later: Sequence[DiveMixtureSchema]
) -> tuple[list[DiveMixtureSchema], dict[int, int]]:
    """A later file of one recording's cylinders joined onto the ones the recording has.

    Returns `(the recording's cylinders, the later file's label -> the recording's)`, the map
    being what the later file's pressure channels and gas switches are rewritten through
    before its channels join the recording's. The mixture half of `extract_recording`'s "each
    value from the first file that recorded it", **per member** rather than per list: the
    corpus pair is a Suunto Ocean JSON whose cylinder carries pressures and no gas fraction
    beside the same computer's FIT, which carries `oxygen` 33 and no pressures, and taking
    the first list whole would drop one of the two.

    **By mix first, then by order**, as `relabel_gas_numbers` matches a second computer's
    list: two rows agreeing on `(oxygen, helium)` are the same tank, and position is the
    fallback where a file records no mix - the Ocean's JSON records none, so its cylinders
    join the FIT's by position. A positional pair whose recorded fractions disagree is not a
    pair. A matched cylinder takes the members it lacks, under `_fill_is_storable`'s rule, and
    a **label** where it has none: the FIT labels nothing where the package has no channel to
    point at, and its JSON's label is then the recording's, whichever file came first. A
    later cylinder nothing matches is one the later file saw, and is appended with its label,
    or the next free one where that is taken, so no channel of it names another tank.
    """
    if not earlier:
        return list(later), {}
    rows = list(earlier)
    remaining = list(range(len(rows)))
    pairs: list[tuple[int, DiveMixtureSchema]] = []
    unmatched: list[DiveMixtureSchema] = []

    by_position: list[DiveMixtureSchema] = []
    for incoming in later:
        match = next(
            (
                index
                for index in remaining
                if rows[index].oxygen is not None
                and incoming.oxygen is not None
                and rows[index].oxygen == incoming.oxygen
                and rows[index].helium == incoming.helium
            ),
            None,
        )
        if match is None:
            by_position.append(incoming)
            continue
        remaining.remove(match)
        pairs.append((match, incoming))
    for incoming in by_position:
        if remaining and not _mixes_disagree(rows[remaining[0]], incoming):
            pairs.append((remaining.pop(0), incoming))
        else:
            unmatched.append(incoming)

    taken = {row.gas_number for row in rows if row.gas_number is not None}

    def free(label: int | None) -> int:
        chosen = label if label is not None and label not in taken else max(taken, default=-1) + 1
        taken.add(chosen)
        return chosen

    labels: dict[int, int] = {}
    for index, incoming in pairs:
        row = rows[index]
        values: dict[str, float | int] = {
            name: reading
            for name in FILLABLE_MIXTURE_FIELDS
            if (reading := getattr(incoming, name)) is not None and getattr(row, name) is None
        }
        if not _fill_is_storable(row, values):
            values = {}
        label = row.gas_number
        if label is None and incoming.gas_number is not None:
            label = values["gas_number"] = free(incoming.gas_number)
        if incoming.gas_number is not None and label is not None:
            labels[incoming.gas_number] = label
        rows[index] = row.model_copy(update=values) if values else row
    for incoming in unmatched:
        label = None if incoming.gas_number is None else free(incoming.gas_number)
        if incoming.gas_number is not None and label is not None:
            labels[incoming.gas_number] = label
        rows.append(incoming.model_copy(update={"gas_number": label}))
    return rows, labels


def _mixes_disagree(stored: IncomingCylinder, incoming: IncomingCylinder) -> bool:
    """Whether two cylinders both record a fraction and record it differently."""
    return any(
        getattr(stored, name) is not None
        and getattr(incoming, name) is not None
        and getattr(stored, name) != getattr(incoming, name)
        for name in ("oxygen", "helium")
    )


def primary_fills(parsed: Sequence[IncomingCylinder], stored: Sequence[DiveMixtureRead]) -> dict[int, dict[str, float]]:
    """What the primary recording's cylinders put into the dive's rows, by row id.

    The pairs `renumber_onto_labels` makes, each filling on `fill_from_pair`'s terms with its
    position trusted: the primary's rows came from its own file.
    """
    pairs, _ = pair_cylinders(parsed, stored)
    return {pair.row.id: values for pair in pairs if (values := fill_from_pair(pair, trust_position=True))}


async def _write_fills(db: AsyncSession, fills: Mapping[int, Mapping[str, float]]) -> None:
    for row_id, values in fills.items():
        await db.execute(update(DiveMixture).where(DiveMixture.id == row_id).values(**values))


async def fill_dive_mixtures(db: AsyncSession, *, dive_id: int, parsed: Sequence[DiveMixtureSchema]) -> None:
    """Fill the dive's rows from a second reading of its primary recording that brings no file.

    The import writer's, for a match onto ordinal 0 whose document it does not keep as a file.
    A match that brings its file fills through `rederive_recording` instead. A second reading
    that brings no file fills nothing of a recording past the first: no check can tell a
    document's second reading from a logbook re-imported over a value the diver cleared, so
    that refill stays where it always was, on the primary.

    Read-then-write rather than a `COALESCE` per column, unlike `fill_tech_scalars`, because
    the pairing is only visible to Python. There is no race to lose: every caller is inside a
    transaction on a dive only its owner can reach.
    """
    from ..crud.crud_dive_mixtures import get_mixtures_for_dive

    if not parsed:
        return
    await _write_fills(db, primary_fills(parsed, await get_mixtures_for_dive(db=db, dive_id=dive_id)))


@dataclass(frozen=True, slots=True)
class StoredRecordingFile:
    """Where an attached file landed: the recording's row id and the file's public uuid.

    Row ids rather than the `RecordingRead` the route answers with, deliberately. This module
    writes; shaping a response is the route's job, and reading one back here would have meant
    a query issued *after* the commit purely to build a return value - a second read that
    could see a concurrent change the write did not make.
    """

    recording_id: int
    file_uuid: uuid_pkg.UUID


async def store_recording_file(
    db: AsyncSession,
    *,
    user_id: int,
    user_uuid: uuid_pkg.UUID,
    dive_id: int,
    upload: UploadFile,
    file_token: str,
) -> StoredRecordingFile:
    """Attach one dive-computer export to this dive, in the recording it belongs to.

    Raises `HTTPException(413)` via `read_upload_within_limit` if the upload is
    oversized and via `ensure_room` if storing it would take the account past its storage
    limit, `InvalidDiveFileTokenError` if it isn't accompanied by a valid parse receipt for
    these exact bytes, and `DiveFileAlreadyLinkedError` if the same content is already
    stored against another dive. A repeat of bytes the dive already has adds nothing and
    is never refused for storage.

    The token is the admission control, unchanged. Sniffing the bytes again here would only
    establish that they *look* readable, which would let this endpoint store any blob shaped
    like an export and would not tie the stored file to the parse that pre-filled the dive's
    form. Checking a signature over the content hash establishes both, and costs one HMAC
    over a digest the dedupe needs anyway; the format id the token names is what the file is
    read and stored as.

    **Where the file lands is the same-recording test's answer**, applied within this dive
    only - the caller has already said which dive these bytes belong to, so the question left
    is which of *its* records they are a second reading of. A match fills that recording's
    blanks and never overwrites them; no match appends a new recording after the last.
    """
    data = await read_upload_within_limit(upload, MAX_DIVE_FILE_SIZE)
    digest = hashlib.sha256(data).hexdigest()
    format_id = _admit(user_uuid=user_uuid, digest=digest, file_token=file_token)

    existing = await _find_by_digest(db, user_id=user_id, digest=digest)
    outcome = reconcile(existing, dive_id)

    if outcome == "conflict" and existing is not None:
        # Deliberately not resolved by re-pointing the row at this dive (which would
        # silently strip the file off the dive that has it) or by storing a second copy
        # (which would defeat the dedupe). The realistic cause is logging one export as
        # two dives, and saying so is more use to the diver than either silent fix.
        other_uuid = (await db.execute(select(Dive.uuid).where(Dive.id == existing.dive_id))).scalar_one_or_none()
        raise DiveFileAlreadyLinkedError(other_uuid)

    if outcome == "noop" and existing is not None:
        return await _repeat_upload(db, existing=existing, data=data, dive_id=dive_id, user_id=user_id)

    # Before the read, so a file the account has no room for costs no extraction; and
    # before the release below, since it reads.
    await _ensure_room_for(db, user_id=user_id, data=data)

    # Deliberately *before* the transaction below, not inside it: an exception raised in
    # there is caught by the `IntegrityError` handler and reported to the diver as a
    # concurrent-upload conflict, which a read failure is not.
    #
    # In a thread for the same reason `POST /dive/parse` reads in one: decoding a FIT file
    # is pure Python, and this is an `async def`. The read transaction is released first so
    # the connection isn't held idle for the duration - see `release_read_transaction`.
    await release_read_transaction(db)
    extraction = await run_in_threadpool(extract_file, data, format_id)

    recordings = await dive_recordings.load_recordings_for_dive(db, dive_id=dive_id)
    incoming = _incoming_facts(extraction)
    matched = next(
        (
            candidate
            for candidate in recordings
            if incoming is not None and dive_recordings.is_same_recording(incoming, candidate.facts)
        ),
        None,
    )

    # The matched recording's existing files, and what the whole set says once these bytes
    # join it - **both read before the transaction opens**, so the write below is writes
    # only. `known` hands over the extraction of the incoming file, which was read a few lines
    # up to decide where it lands: without it this would read it a second time.
    filename = safe_filename(upload.filename, default="dive-file")
    now = datetime.now(UTC)
    content_type = content_type_of(format_id)
    incoming_file = LoadedDiveFile(
        data=data,
        content_type=content_type,
        original_filename=filename,
        sha256=digest,
        parser_key=format_id,
    )
    existing_files = [] if matched is None else await load_recording_files(db, recording_id=matched.id)
    # Appended last, which is where `ORDER BY dive_file.id` will put it once the row lands -
    # and attach order is the whole of what "the first file that recorded it" means.
    files = [*existing_files, incoming_file]
    # Released a *second* time, because the two reads above reopened a transaction the first
    # release had closed and nothing has been written yet. Without this the connection sits
    # idle-in-transaction across the read of every file already on the recording and the blob
    # write below - which is the pool exhaustion `release_read_transaction` exists to prevent,
    # arrived at from the other side. Safe for the reason that function documents: everything
    # this still needs (`matched`, `files`) is a frozen dataclass, detached from the session.
    await release_read_transaction(db)
    # The recording's stored start is the axis origin: the matched one's, or - for the
    # recording this upload creates - the incoming file's own, which is what it will store.
    origin = matched.facts if matched is not None else incoming
    recording_extraction = await run_in_threadpool(
        extract_recording,
        files,
        {digest: extraction},
        start_time=None if origin is None else origin.start_time,
        utc_offset_minutes=None if origin is None else origin.utc_offset_minutes,
    )
    # The file lands on the volume *before* the transaction that references it. Every
    # database-visible state therefore names bytes that exist; the only thing a crash
    # between the two can produce is an unreferenced file, which is harmless until the
    # sweeper reclaims it. The key carries a nonce minted per write - see
    # `blob_store.new_key`.
    storage_key = blob_store.new_key(KEY_KIND, sha256=digest)
    stored_byte_size = await blob_store.put(storage_key, data)

    try:
        if matched is not None:
            recording_id, ordinal = matched.id, matched.ordinal
            await dive_recordings.fill_device_fields(
                db, recording_id=recording_id, device=None if extraction.parsed is None else extraction.parsed.device
            )
            await dive_recordings.fill_recording_settings(
                db,
                recording_id=recording_id,
                mode=None if extraction.parsed is None else extraction.parsed.mode,
                deco_model=None if extraction.parsed is None else extraction.parsed.deco_model,
                salinity=None if extraction.parsed is None else extraction.parsed.salinity,
            )
            if incoming is not None:
                await dive_recordings.fill_start(
                    db,
                    recording_id=recording_id,
                    start_time=incoming.start_time,
                    utc_offset_minutes=incoming.utc_offset_minutes,
                )
        else:
            ordinal = await dive_recordings.next_ordinal(db, dive_id=dive_id)
            recording_id = await dive_recordings.create_recording(
                db,
                dive_id=dive_id,
                user_id=user_id,
                ordinal=ordinal,
                device=None if extraction.parsed is None else extraction.parsed.device,
                mode=None if extraction.parsed is None else extraction.parsed.mode,
                deco_model=None if extraction.parsed is None else extraction.parsed.deco_model,
                salinity=None if extraction.parsed is None else extraction.parsed.salinity,
                start_time=None if incoming is None else incoming.start_time,
                utc_offset_minutes=None if incoming is None else incoming.utc_offset_minutes,
                duration=None if incoming is None else incoming.duration,
                max_depth=None if incoming is None else incoming.max_depth,
            )

        file_uuid = uuid7()
        await db.execute(
            insert(DiveFile).values(
                user_id=user_id,
                recording_id=recording_id,
                dive_id=dive_id,
                sha256=digest,
                content_type=content_type,
                byte_size=len(data),
                stored_byte_size=stored_byte_size,
                original_filename=filename,
                parser_key=format_id,
                storage_key=storage_key,
                # Spelled out rather than left to `PublicUUIDMixin`'s `default_factory`:
                # that is a dataclass-level default applied when the ORM constructs an
                # instance, and this Core-level INSERT never constructs one.
                uuid=file_uuid,
                created_at=now,
            )
        )
        await rederive_recording(
            db,
            recording_id=recording_id,
            dive_id=dive_id,
            ordinal=ordinal,
            change=RecordingChange.CREATED if matched is None else RecordingChange.JOINED,
            files=files,
            extraction=recording_extraction,
        )
        await db.commit()
    except IntegrityError as exc:
        # Two uploads for one recording raced. One user per dive and a button disabled while
        # in flight make this vanishingly rare; a retry is a better answer than a lock on the
        # hot path. The file written above is left on the volume: no row references it, so it
        # is an orphan for the sweeper, and a retry mints a new key and writes again.
        await db.rollback()
        raise DiveFileConflictError(
            "This dive's recordings changed while this upload was in flight. Please try again."
        ) from exc

    return StoredRecordingFile(recording_id=recording_id, file_uuid=file_uuid)


def _admit(*, user_uuid: uuid_pkg.UUID, digest: str, file_token: str) -> str:
    """The token checks, in one place. Returns the format id the receipt names."""
    claims = verify_dive_file_token(file_token)
    if claims is None:
        raise InvalidDiveFileTokenError("This import has expired. Re-import the file to attach it.")
    if claims.user_uuid != str(user_uuid):
        raise InvalidDiveFileTokenError("This import belongs to a different account. Re-import the file to attach it.")
    if claims.sha256 != digest:
        raise InvalidDiveFileTokenError("This file doesn't match the one that was imported. Re-import it to attach it.")

    # A format this build's reader does not name means the token outlived a reader being
    # renamed or removed, and there is nothing to read the file as.
    if not reads(claims.parser_key):
        raise InvalidDiveFileTokenError("This import is no longer supported. Re-import the file to attach it.")
    return claims.parser_key


def _incoming_facts(extraction: FileExtraction) -> dive_recordings.RecordingFacts | None:
    """The gates' view of a file just read, or `None` when it named no start.

    **`duration` and `max_depth` here are the samples' span and deepest reading**, the
    import's rule for the same two columns, so a recording carries figures by one rule
    whichever door it came in by. The sampled span rides along separately in milliseconds,
    because the same-recording gate compares that.

    A file that recorded no start time cannot be matched or placed on the clock at all, so it
    gets a recording of its own with a NULL start - which is what a header-only export with
    no timestamp is.
    """
    if extraction.start is None:
        return None
    start_time, offset_minutes = extraction.start
    duration, max_depth = gate_figures(extraction.profile)
    return dive_recordings.RecordingFacts(
        device=dive_recordings.device_of(None if extraction.parsed is None else extraction.parsed.device),
        start_time=start_time,
        utc_offset_minutes=offset_minutes,
        duration=duration,
        max_depth=max_depth,
        sampled_span=None if extraction.profile is None else extraction.profile.duration,
    )


async def read_recording(
    db: AsyncSession,
    *,
    recording_id: int,
    known: Mapping[str, FileExtraction] | None = None,
    release: bool = False,
) -> tuple[list[LoadedDiveFile], RecordingExtraction]:
    """A recording's files and what they say, with the reading done **off the event loop**.

    The seam that keeps `extract_recording` - pure CPU, a FIT's decode above all - out of the
    request's own thread, which is what *"Uploaded files are parsed in a thread, not on the
    event loop"* in `DECISIONS.md` requires.

    **`release` says the caller has nothing left in the transaction to lose**, and it has to be
    the caller's answer rather than this function's. `release_read_transaction` rolls back, so
    it can free the connection for the length of the read only where nothing has been written
    yet - which is true of `_repeat_upload`, whose reads are all lookups, and false of
    `refresh_tech_scalars`, which every caller reaches after a delete or a promotion has
    already been issued. There the connection is held for the duration, and that is the
    accepted cost: what is bought either way is the event loop, which is the scarce thing.
    """
    files = await load_recording_files(db, recording_id=recording_id)
    if not files:
        return [], RecordingExtraction()
    start_time, utc_offset_minutes = await dive_recordings.recording_start(db, recording_id=recording_id)
    if release:
        await release_read_transaction(db)
    return files, await run_in_threadpool(
        extract_recording, files, known, start_time=start_time, utc_offset_minutes=utc_offset_minutes
    )


class RecordingChange(Enum):
    """What just happened to a recording's files - the one thing `rederive_recording` is told.

    Two answers hang off it and neither can be read off the rows: whether the recording had
    anything to lose, and whether something arrived that may fill the dive's cylinders.
    """

    # This upload or import created the recording, with this file.
    CREATED = "created"
    # New bytes joined a recording that already existed.
    JOINED = "joined"
    # The recording's own bytes read again: a repeat upload, the profile backfill.
    REREAD = "reread"
    # A file came off the recording.
    REMOVED = "removed"


async def rederive_recording(
    db: AsyncSession,
    *,
    recording_id: int,
    dive_id: int,
    ordinal: int,
    change: RecordingChange,
    files: Sequence[LoadedDiveFile],
    extraction: RecordingExtraction,
) -> None:
    """Rewrite everything derived from one recording's files. **Writes only.**

    Called after any change to a recording's files, with the files and their extraction from
    `read_recording` - which is what keeps the reading off the event loop and out of this
    function entirely - and by `backfill_profiles` over a recording whose profile is behind.
    Deriving from *all* of them, rather than folding the new one into what is stored, is what
    makes the fill rule mean the same thing on every path: the answer is a function of the
    files in attach order and of nothing else, so an attach, a deletion and a backfill run
    cannot disagree about it.

    **The cylinders are labelled first** (`label_cylinders`), because the profile about to be
    stored names them by label. **The recording's gate figures are rewritten outright** from
    the samples being stored - derived columns nobody edits, and the import's rule for them -
    so a recording stored with a device's logged figures comes out on the samples' span the
    first time anything re-reads its bytes.

    **Every recording's readouts are its own files'**, written before anything the primary
    alone may write. **The dive's fixes are the primary recording's, and only the primary's**:
    a secondary recording is a second computer's account of the same dive, and its positions
    are not the dive's.

    **`change` is the caller's answer, and two things follow from it.** A recording `CREATED`
    or with a file `REMOVED` has nothing to lose, so its readouts and the fixes are written
    outright, clearing what nothing yields; otherwise they fill. That is true of a recording
    this very upload created and **false of a file-less one a logbook import created
    earlier**, whose first file is a second reading of a record the logbook already holds and
    `JOINED` it - and deliberately not "had no files a moment ago", since a deletion is there
    to stop claiming a reading the remaining files no longer yield.

    **The dive's cylinders fill at an arrival and at nothing else**: new bytes `JOINED` to any
    recording, or a recording past the first `CREATED`. Not the file that creates the primary,
    whose cylinders are the ones the form saved from it; and not a `REREAD` or a `REMOVED`,
    which bring nothing new - filling there puts back a value the diver cleared, read straight
    off the very file they were editing away from, on the next repeat upload or backfill.
    Required rather than defaulted, so a caller added later has to say which it is.
    """
    if not files:
        await delete_profile_for_recording(db, recording_id=recording_id, commit=False)
        return

    fresh = change in (RecordingChange.CREATED, RecordingChange.REMOVED)
    profile = await label_cylinders(
        db,
        dive_id=dive_id,
        recording_id=recording_id,
        ordinal=ordinal,
        mixtures=extraction.mixtures,
        profile=extraction.profile,
        fill=change is RecordingChange.JOINED or (change is RecordingChange.CREATED and ordinal != 0),
    )

    if profile is None:
        await delete_profile_for_recording(db, recording_id=recording_id, commit=False)
    else:
        await store_profile(
            db,
            recording_id=recording_id,
            dive_id=dive_id,
            profile=profile,
            source_sha256=recording_source_digest([file.sha256 for file in files]),
            parser_key=files[0].parser_key,
            reader_version=READER_VERSION,
            commit=False,
        )
    duration, max_depth = gate_figures(profile)
    await dive_recordings.store_gate_figures(db, recording_id=recording_id, duration=duration, max_depth=max_depth)

    if fresh:
        await dive_recordings.store_readouts(db, recording_id=recording_id, readouts=extraction.scalars)
    else:
        await dive_recordings.fill_readouts(db, recording_id=recording_id, readouts=extraction.scalars)

    if ordinal != 0:
        return
    if fresh:
        await store_tech_scalars(db, dive_id=dive_id, scalars=extraction.scalars, commit=False)
    else:
        await fill_tech_scalars(db, dive_id=dive_id, scalars=extraction.scalars)


def renumber_onto_labels(
    parsed: Sequence[DiveMixtureSchema], stored: Sequence[DiveMixtureRead]
) -> tuple[dict[int, int | None], dict[int, int]] | None:
    """A primary recording's cylinder labels onto the dive's own rows, or `None` where they agree.

    Returns `(the label each stored row takes, by row id; old label -> new, for the dive's other
    recordings)`. Pure and DB-free, beside `relabel_gas_numbers`, which answers the other
    direction for a recording past the first over the same pairs (`pair_cylinders`), a matched
    row taking the label the reader gave its cylinder.

    **The dive's cylinders take the reader's labels, and not the other way round**, so the
    stored dive is what the reader produces and every later re-derivation reproduces it -
    mapping the reader's labels onto the stored rows instead would make a primary's profile a
    function of the dive's editable rows rather than of its bytes. A row the reader's list
    does not match keeps its label unless a new label claims it, and then it is cleared: two
    rows sharing a label would join one channel to both. Nothing is appended, since a
    cylinder the diver removed stays removed, and a channel naming it keeps naming nothing.

    The map is what keeps the dive's other recordings joined to the rows they meant: each
    label the renumbering replaced is rewritten through it, a cleared row's to a label no row
    carries - a channel naming no cylinder rather than the wrong one.
    """
    pairs, _ = pair_cylinders(parsed, stored)

    labels: dict[int, int | None] = {row.id: row.gas_number for row in stored}
    claimed: dict[int, int] = {}
    for pair in pairs:
        if pair.incoming.gas_number is not None:
            labels[pair.row.id] = pair.incoming.gas_number
            claimed[pair.incoming.gas_number] = pair.row.id
    for row in stored:
        label = labels[row.id]
        if label is not None and claimed.get(label, row.id) != row.id:
            labels[row.id] = None

    if all(labels[row.id] == row.gas_number for row in stored):
        return None

    highest = max(
        (label for label in (*labels.values(), *(row.gas_number for row in stored)) if label is not None), default=-1
    )
    siblings: dict[int, int] = {}
    for row in stored:
        old, new = row.gas_number, labels[row.id]
        if old is None or old == new:
            continue
        if new is None:
            highest += 1
            new = highest
        siblings[old] = new
    return labels, siblings


async def label_cylinders(
    db: AsyncSession,
    *,
    dive_id: int,
    recording_id: int,
    ordinal: int,
    mixtures: Sequence[DiveMixtureSchema],
    profile: NormalizedProfile | None,
    fill: bool,
) -> NormalizedProfile | None:
    """Join a re-derived recording's cylinder labels to the dive's, and at an arrival fill the
    blanks of the rows they pair. Returns the profile to store.

    **The one labelling, run by every path that stores an extraction read from bytes** - an
    attach, a repeat upload, a file delete, and the backfill through the re-derivation - so a
    pressure channel names a cylinder the dive has whichever of them wrote it. `fill` is
    `rederive_recording`'s answer to whether this is an arrival; the pairs it fills are the
    labelling's own, so a channel's cylinder and the row its values land in are one row.

    **A primary recording's labels are the dive's.** Where the reader's labels and the dive's
    rows disagree - a dive saved under a previous reader's labels, or a form a previous build
    prefilled - the dive's rows are renumbered onto the reader's (`renumber_onto_labels`), and
    every other recording's stored profile is rewritten through the same map, samples it has
    no bytes for included, so no sibling points at a label the renumbering replaced.
    **A recording past the first is mapped onto the dive's cylinders instead**
    (`relabel_gas_numbers`), a cylinder the dive's list lacks appended and a matched row with
    no label given one: `gas_number` is dive-scoped, and a second computer numbers its tanks
    its own way.
    """
    from ..crud.crud_dive_mixtures import get_mixtures_for_dive, replace_mixtures_for_dive

    if not mixtures:
        return profile
    stored = await get_mixtures_for_dive(db=db, dive_id=dive_id)

    if ordinal != 0:
        mapping, cylinders = relabel_gas_numbers(mixtures, stored, fill=fill)
        if cylinders is not None:
            await replace_mixtures_for_dive(db=db, dive_id=dive_id, mixtures=cylinders, commit=False)
        return apply_gas_mapping(profile, mapping)

    if fill:
        await _write_fills(db, primary_fills(mixtures, stored))
    renumbering = renumber_onto_labels(mixtures, stored)
    if renumbering is None:
        return profile
    labels, siblings = renumbering
    for row in stored:
        if labels[row.id] != row.gas_number:
            await db.execute(update(DiveMixture).where(DiveMixture.id == row.id).values(gas_number=labels[row.id]))
    others = (
        await db.execute(
            select(DiveRecording.id).where(DiveRecording.dive_id == dive_id, DiveRecording.id != recording_id)
        )
    ).scalars()
    for other in list(others):
        stored_profile = await load_stored_profile(db, recording_id=other)
        if stored_profile is None:
            continue
        remapped = apply_gas_mapping(stored_profile.profile, siblings)
        if remapped is not None and remapped != stored_profile.profile:
            await replace_profile_samples(db, recording_id=other, profile=remapped)
    return profile


async def refresh_tech_scalars(db: AsyncSession, *, dive_id: int, touched_primary: bool) -> None:
    """Rewrite a dive's entry and exit fixes from its primary recording's files, outright.

    The repair after anything that changes *which* recording is primary or which files it
    holds: a file deleted, a recording deleted, a recording promoted. Outright rather than
    filled, because the point is to stop claiming a reading the dive no longer has any
    evidence for - the same reasoning `delete_dive_file` has always applied, now asked of the
    primary recording rather than of the dive.

    A dive whose primary recording has no files - or which has no recordings at all - has its
    readings cleared, which is what "nothing here can re-derive them" means.

    **`touched_primary` says the change reached the primary recording, and it is the caller's
    answer rather than anything readable here.** By the time this runs the delete has been
    issued and the ordinals renumbered, so the row that would answer it is gone. False makes
    this a no-op, and that is the whole of the guard: an outright rewrite is only ever owed
    where the primary's identity or its files just moved. Run against a dive whose primary was
    untouched it is not a slow no-op but a loss - on a **file-less** primary, the shape a
    logbook import creates from a document, the extraction is empty and every field is written
    `None`, clearing figures a document supplied that nothing on this instance can re-derive;
    and on a primary that *has* files, clearing any field the document supplied and the files
    do not yield. Either way the recording the diver actually deleted had no bearing on them.
    `POST /dives/merge` skips this call outright for the same reason - see *"The dive's own
    `start_time` is not touched, and neither are its oxygen-exposure readings"* in
    `DECISIONS.md`. Required rather than defaulted, so a fourth caller has to answer it.
    """
    if not touched_primary:
        return

    # Never `release=True`: every caller reaches this after a delete or a promotion has been
    # issued, and rolling the transaction back to free the connection would discard them.
    primary = (await dive_recordings.primary_recording_ids(db, dive_ids=[dive_id])).get(dive_id)
    scalars = {} if primary is None else (await read_recording(db, recording_id=primary))[1].scalars
    await store_tech_scalars(db, dive_id=dive_id, scalars=scalars, commit=False)


async def _repeat_upload(
    db: AsyncSession, *, existing: _ExistingRow, data: bytes, dive_id: int, user_id: int
) -> StoredRecordingFile:
    """The same bytes, already stored against this dive. Idempotent, with two repairs.

    Nothing is rewritten, not even `original_filename`: the bytes are the file's identity,
    and re-uploading them is the client repeating itself.

    The *profile*, though, is a function of (these bytes, the extractor version, the reader
    version), so a repeated attach after either version moved opportunistically upgrades it
    from bytes already in hand. The tech scalars ride that same gate, having none of their
    own, which makes a reader fix that changes only scalars invisible here -
    `backfill_tech_fields` is what picks those up.

    And re-uploading is the natural repair after a partial loss of the blob store, on
    either backend: without the `has`/`put` below the row says "already stored", the
    download 500s forever, and the server refuses the very bytes that would fix it. `has`
    decodes a compressed object, so a frame cut short is repaired as a lost file is. The
    write re-`put`s the key the row already carries rather than minting one, which writes an
    object decoding to these same bytes - not necessarily the same frame, since libzstd
    versions may encode one input differently, so the row's `stored_byte_size` is written
    again from what `put` reports. That write commits on its own, straight away: the
    re-extraction below releases with a rollback before it writes anything, and the paths
    that skip it commit nothing at all.

    **The blob repair runs first, before anything reads the recording's files.** It has to:
    re-deriving a recording loads every file it holds, and `load_recording_files` raises
    `BlobMissingError` on bytes that are gone - so repairing second would make this path fail
    on exactly the condition it exists to fix. That ordering did not matter while a dive had
    one file and the re-extraction read the bytes it had just been handed; it matters now,
    and nothing but a test would have caught it.
    """
    if not await blob_store.has(existing.storage_key):
        logger.warning("Rewriting the missing stored file for dive %s from a re-upload", dive_id)
        stored_byte_size = await blob_store.put(existing.storage_key, data)
        await db.execute(update(DiveFile).where(DiveFile.id == existing.id).values(stored_byte_size=stored_byte_size))
        await db.commit()

    ordinal = (
        await db.execute(select(DiveRecording.ordinal).where(DiveRecording.id == existing.recording_id))
    ).scalar_one_or_none()
    try:
        digests = [file.sha256 for file in await load_recording_files(db, recording_id=existing.recording_id)]
    except blob_store.BlobMissingError:
        # A *sibling* file of this recording is missing, which these bytes cannot repair. The
        # upload is still a no-op and still the right answer; what is skipped is the
        # opportunistic upgrade, and `backfill_profiles` reports the recording when it
        # reaches it.
        logger.error("Skipping re-extraction for recording %s: a sibling file is missing", existing.recording_id)
        return StoredRecordingFile(recording_id=existing.recording_id, file_uuid=existing.uuid)

    if digests and should_extract(
        await get_existing_profile(db, recording_id=existing.recording_id),
        sha256=recording_source_digest(digests),
    ) == ("extract"):
        # `release=True`: nothing above this is left in the transaction - the blob repair's
        # size is already committed - so the connection is freed for the read rather than
        # pinned across it.
        files, extraction = await read_recording(db, recording_id=existing.recording_id, release=True)
        try:
            await rederive_recording(
                db,
                recording_id=existing.recording_id,
                dive_id=dive_id,
                ordinal=ordinal or 0,
                change=RecordingChange.REREAD,
                files=files,
                extraction=extraction,
            )
            # One commit for the profile and the scalars: they come out of the same bytes,
            # and a dive whose exposure readings were upgraded but whose profile wasn't would
            # be describing two different extractions. The writes are inside the `try`, not
            # just the commit: a `CHECK` is not deferrable in Postgres, so `IntegrityError` is
            # raised from `execute()` rather than from `commit()`.
            await db.commit()
        except IntegrityError:
            # An opportunistic upgrade of bytes the recording already has, so failing it must
            # not fail the request. Rolled back explicitly - without it the session stays in a
            # failed transaction and the *next* statement on it dies somewhere unrelated.
            logger.exception("Opportunistic re-extraction for recording %s could not be stored", existing.recording_id)
            await db.rollback()

    return StoredRecordingFile(recording_id=existing.recording_id, file_uuid=existing.uuid)


async def load_dive_file(db: AsyncSession, *, file_id: int) -> LoadedDiveFile | None:
    """Fetch one stored export, or `None` if the row is gone.

    `None` means *there is no such row*. A row whose file is missing from the volume raises
    `blob_store.BlobMissingError` instead, and each caller decides how loudly to fail: the
    download route 500s, the export archive skips the member, the backfills count it failed.
    Collapsing the two into `None` would report data loss as a 404.
    """
    stmt = select(
        DiveFile.storage_key,
        DiveFile.content_type,
        DiveFile.original_filename,
        DiveFile.sha256,
        DiveFile.parser_key,
    ).where(DiveFile.id == file_id)
    row = (await db.execute(stmt)).one_or_none()
    if row is None:
        return None

    return LoadedDiveFile(
        data=await blob_store.get(row.storage_key),
        content_type=row.content_type,
        original_filename=row.original_filename,
        sha256=row.sha256,
        parser_key=row.parser_key,
    )


async def load_recording_files(
    db: AsyncSession, *, recording_id: int, held: Mapping[str, Callable[[], bytes]] | None = None
) -> list[LoadedDiveFile]:
    """Every file of one recording, in attach order, with its bytes.

    **Attach order is `dive_file.id`** - a later upload takes a higher sequence value - and
    it is the whole of what "the first file that recorded it" means. Every caller of
    `extract_recording` gets its input from here so that ordering is stated once.

    `held` reads the bytes of a row whose object is not in the store yet, by storage key:
    logbook import names every file it stores in rows first and writes the objects once the
    whole import has fitted the account, so a recording it gives a second file is re-read
    from the import's own files.

    Raises `BlobMissingError` if any of the bytes are gone, on `load_dive_file`'s terms: a
    partial read would silently apply the fill rule to a subset of the files and store the
    result as though it were the whole.
    """
    held = held or {}
    stmt = (
        select(
            DiveFile.storage_key,
            DiveFile.content_type,
            DiveFile.original_filename,
            DiveFile.sha256,
            DiveFile.parser_key,
        )
        .where(DiveFile.recording_id == recording_id)
        .order_by(DiveFile.id)
    )
    return [
        LoadedDiveFile(
            data=read() if (read := held.get(row.storage_key)) is not None else await blob_store.get(row.storage_key),
            content_type=row.content_type,
            original_filename=row.original_filename,
            sha256=row.sha256,
            parser_key=row.parser_key,
        )
        for row in (await db.execute(stmt)).all()
    ]


async def resolve_dive_file(db: AsyncSession, *, dive_id: int, uuid: uuid_pkg.UUID) -> tuple[int, int]:
    """One of this dive's files by public uuid, as `(file id, recording id)`.

    Scoped to the dive rather than looked up globally, so a uuid belonging to another dive is
    indistinguishable from one that does not exist - `fetch_owned_or_raise`'s 404-not-403,
    applied a level down.
    """
    row = (
        await db.execute(
            select(DiveFile.id, DiveFile.recording_id).where(DiveFile.dive_id == dive_id, DiveFile.uuid == uuid)
        )
    ).one_or_none()
    if row is None:
        raise DiveFileNotFoundError("This dive has no such file.")
    return int(row.id), int(row.recording_id)


async def get_dive_file_sha256(db: AsyncSession, *, file_id: int) -> str | None:
    """Fetch just one stored export's content hash, without touching its bytes.

    Lets the download route answer a conditional request (`If-None-Match`) with a 304
    after a single narrow query, instead of pulling megabytes off the volume only to
    discard them.
    """
    stmt = select(DiveFile.sha256).where(DiveFile.id == file_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def delete_dive_file(db: AsyncSession, *, file_id: int, commit: bool = True) -> None:
    """Remove one stored export and re-derive whatever was read off it.

    Hard, not soft, and not merely unlinked: a row nothing can reach keeps its file on the
    volume forever, and a "delete" that only hides the file would be a worse trade than
    losing it from the corpus. The file itself goes after the caller's transaction commits -
    the mirror of the write ordering in `store_recording_file`.

    **What happens to the recording depends on what is left and on where its profile came
    from.** A recording whose last file goes normally goes with it: its profile was only ever
    read off that file and can never be re-derived or checked against anything. But a profile
    whose provenance is `merge` or `divejson_import` is one *no file can re-yield*, so there
    the recording and its samples survive their last file's deletion - decision-9 principle,
    the same one both backfills follow. `POST /dives/merge` is what makes that reachable: a
    merged recording keeps whatever files either part had.

    The *mixtures* are pointedly not touched: those went through the form, the diver may have
    edited them since, and they are the dive's own record rather than the file's.

    **The dive's readings are re-derived only where the file came off the primary recording.**
    A secondary recording is a second computer's account of the same dive and the dive's
    oxygen-exposure figures were never read off it, so emptying one has nothing to say about
    them - see `refresh_tech_scalars` on what running it anyway costs.
    """
    row = (
        await db.execute(
            select(DiveFile.recording_id, DiveFile.dive_id, DiveFile.storage_key).where(DiveFile.id == file_id)
        )
    ).one_or_none()
    if row is None:
        raise DiveFileNotFoundError("This dive has no such file.")

    await db.execute(delete(DiveFile).where(DiveFile.id == file_id))
    blob_store.delete_after_commit(db, [row.storage_key])

    remaining = await load_recording_files(db, recording_id=row.recording_id)
    ordinal = (
        await db.execute(select(DiveRecording.ordinal).where(DiveRecording.id == row.recording_id))
    ).scalar_one_or_none()
    start_time, utc_offset_minutes = await dive_recordings.recording_start(db, recording_id=row.recording_id)
    # Read before the branch below can delete the recording, which is the only moment the
    # question is still answerable. `None` cannot happen - the file row named a live
    # recording a statement ago - and is read as the primary so that a row that somehow
    # vanished still gets the repair rather than silently skipping it.
    touched_primary = (ordinal or 0) == 0

    if remaining:
        await rederive_recording(
            db,
            recording_id=row.recording_id,
            dive_id=row.dive_id,
            ordinal=ordinal or 0,
            change=RecordingChange.REMOVED,
            files=remaining,
            extraction=await run_in_threadpool(
                extract_recording, remaining, start_time=start_time, utc_offset_minutes=utc_offset_minutes
            ),
        )
    else:
        existing = await get_existing_profile(db, recording_id=row.recording_id)
        if existing is not None and not existing.is_reproducible:
            # Samples nothing here can produce again. The recording stays, file-less, which
            # is the same first-class shape a logbook import creates from a document.
            pass
        else:
            await dive_recordings.delete_recording(db, recording_id=row.recording_id, dive_id=row.dive_id, commit=False)
        await refresh_tech_scalars(db, dive_id=row.dive_id, touched_primary=touched_primary)

    if commit:
        await db.commit()


async def delete_files_for_dive(db: AsyncSession, *, dive_id: int, commit: bool = True) -> None:
    """Hard-delete every recording of a dive, and with them its files and profiles.

    The dive-deletion hook. The FK's `ON DELETE CASCADE` from `dive` can't do this for us:
    deletion is application-level (`is_deleted`), so no `DELETE FROM dive` ever runs and that
    cascade never fires - the same reasoning as `delete_files_for_certification`. The cascade
    from `dive_recording` *does* fire, which is why removing the recordings is enough to take
    the files and the profiles with them.
    """
    # By dive rather than through the recordings, which is a strict superset and one query
    # fewer - and the keys have to be in hand before the cascade removes the rows naming them.
    keys = list((await db.execute(select(DiveFile.storage_key).where(DiveFile.dive_id == dive_id))).scalars())
    await db.execute(delete(DiveRecording).where(DiveRecording.dive_id == dive_id))
    # The cascade has already taken these, and the two statements below cost one empty
    # `DELETE` each on every ordinary path. They stay because `recording_id` is the *only*
    # thing tying a file or a profile to a recording, and a row that lost its recording by
    # any route the cascade does not cover would otherwise outlive the dive it belongs to -
    # keeping its slot in `ux_dive_file_user_id_sha256` and blocking a re-import of the same
    # export into a fresh dive, which is the failure this function exists to prevent.
    #
    # Through `delete_profiles_for_dive` rather than a `DELETE` written out here, so that
    # `models/dive_profile.py`'s "what actually removes these rows" is a function a reader can
    # find. It said so while the statement was inline, which made the docstring false and the
    # function dead in one move.
    await delete_profiles_for_dive(db, dive_id=dive_id, commit=False)
    await db.execute(delete(DiveFile).where(DiveFile.dive_id == dive_id))
    blob_store.delete_after_commit(db, keys)
    await store_tech_scalars(db, dive_id=dive_id, scalars={}, commit=False)
    if commit:
        await db.commit()


@dataclass(frozen=True, slots=True)
class TechBackfillReport:
    """What one run of `backfill_tech_fields` did.

    Recordings and mixtures are counted separately because they are backfilled on different
    terms - a recording's readouts, device and settings are filled where its files yield a
    value it lacks, the mixture fields are applied only where the stored rows still
    demonstrably describe the parsed ones - so one number could not say whether a run went
    well. `mixtures_skipped` in particular is the interesting count: it is the diver having
    edited their cylinders since the import, which is a reason not to touch them rather than
    a failure.
    """

    examined: int = 0
    recordings_updated: int = 0
    mixtures_updated: int = 0
    mixtures_skipped: int = 0
    failed: int = 0


def merge_mixture_fields(
    parsed: list[DiveMixtureSchema], stored: list[DiveMixtureRead]
) -> list[tuple[int, dict[str, object]]] | None:
    """Line parsed mixtures up with stored ones, or refuse to.

    `(id, values)` per row to update, or `None` when the two lists can't be shown to
    describe the same cylinders. Pure and DB-free, following the `reconcile()` idiom in
    this module - the decision worth testing is testable without a database. A row the
    file says nothing about is absent from the result rather than present with an empty
    dict, so `[]` is a legitimate answer meaning "matched, nothing to write".

    Position is the only available join: mixtures are replaced wholesale on every save
    (`crud_dive_mixtures.replace_mixtures_for_dive`), so a stored row's `id` is newer than
    the import and says nothing about which parsed cylinder it came from. Position alone
    is too weak to trust on its own, though - a diver who deleted their deco bottle and
    added a different one would have the parsed second gas written onto it. So the counts
    must match **and** every pair must still agree on the `(oxygen, helium)` both sides
    recorded, which is the part of a cylinder a diver has no reason to retype and every
    reason to leave alone.

    Because the join is positional, **`stored` must be in the order the cylinders were
    saved in**, which is what `get_mixtures_for_dive`'s `ORDER BY id` guarantees and
    nothing in this function can check. The `(oxygen, helium)` agreement above is not a
    backstop for a mis-ordered list either: a file that records no fractions at all leaves
    both `None` on every row, and `None` is explicitly not evidence of a mismatch (below) -
    on the parsed side or, since the columns became nullable, on the stored one. A 2026
    Suunto Ocean's JSON export is exactly that shape: its cylinders come from sample data,
    which carries pressures and no gas block.

    All-or-nothing per dive, not per row: a list that half-matches is a list that has been
    edited, and half-applying to it would leave a set of cylinders that came from two
    different places with nothing recording which is which.
    """
    if len(parsed) != len(stored) or not parsed:
        return None

    updates: list[tuple[int, dict[str, object]]] = []
    for parsed_mix, stored_mix in zip(parsed, stored, strict=True):
        # `None` on **either** side means that side never recorded a fraction, which is
        # not evidence of a mismatch and not evidence of a match either. Only fractions
        # both sides actually recorded are compared.
        #
        # The parsed side was the whole of this rule while the stored side could not be
        # null: a file that recorded nothing could not be checked against the default the
        # form had filled in. `dive_mixture.oxygen` and `.helium` are nullable now, and
        # the import path stores that absence rather than defaulting it, so a stored row
        # can say "not recorded" in the same way - and a one-sided guard would read
        # `parsed 21` against `stored NULL` as two different gases and refuse the whole
        # dive. That would fall on exactly the mix-less imports this change exists for.
        #
        # It does widen the window the docstring's ordering warning names: a pair with
        # nothing recorded on one side agrees by default, so the positional join carries
        # more of the weight there. The `ORDER BY id` that join depends on is what keeps
        # it honest, which is why it is a precondition rather than a nicety.
        if parsed_mix.oxygen is not None and stored_mix.oxygen is not None and parsed_mix.oxygen != stored_mix.oxygen:
            return None
        if parsed_mix.helium is not None and stored_mix.helium is not None and parsed_mix.helium != stored_mix.helium:
            return None
        # **Fill-only: a parsed `None` is dropped, not written.** This is the same rule as
        # the fraction comparison above, applied to the write instead of the guard.
        # `DiveMixtureSchema`'s whole premise is that `None` means "the file did not record
        # this" rather than "this is nothing" - so spreading one into an `UPDATE` turns the
        # absence of a reading into a value, which is the exact conflation that schema
        # exists to prevent.
        #
        # It is also silent data loss, because both of these are client-writable
        # (`DiveMixtureBase` -> `DiveMixtureCreate`, and `PATCH /dive/{uuid}` replaces
        # mixtures wholesale). A FIT import produces `po2_limit=None` always and
        # `role=None` for any open-circuit gas; a diver who then sets 1.6 and `deco` on
        # their stage bottle has touched neither fraction, so the guard above still admits
        # the join and the backfill would have written both back to `NULL`.
        #
        # The dive's own scalars are overwritten outright a few lines up, and the asymmetry
        # is the point: nothing but the import writes those, so there is no edit to lose.
        # These two have another writer.
        #
        # A reader *correction* still lands, which is what the fill-only rule costs and
        # doesn't: where the file records a value the backfill overwrites as before, and it
        # declines only where the file has nothing to say.
        #
        # **`gas_number` is not among them.** This join is positional, and a label written
        # by it would put the reader's labels on an old dive by a rule other than the
        # labelling's - run before the profile backfill, it would break the dive's join to
        # its stored channels until that backfill reached the dive. A label is the profile
        # path's to write (`label_cylinders`).
        # Annotated rather than inferred: `dict` is invariant in its value type, so the
        # comprehension's own `dict[str, float | GasRole]` is not a `dict[str, object]`.
        values: dict[str, object] = {
            name: value
            for name, value in (
                ("po2_limit", parsed_mix.po2_limit),
                ("role", parsed_mix.role),
            )
            if value is not None
        }
        if values:
            updates.append((stored_mix.id, values))
    return updates


# How many dives are processed between commits. Mirrors `_BACKFILL_BATCH_SIZE` in
# `services/dive_profiles.py`, for the same trade: small enough that an interrupted run
# loses little, large enough that a few hundred dives isn't a few hundred transactions.
_BACKFILL_BATCH_SIZE = 50


async def backfill_tech_fields(
    db: AsyncSession,
    *,
    parser_key: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> TechBackfillReport:
    """Re-read every recording's stored files for the columns nothing else fills.

    A second script rather than an extension of `backfill_profiles`, because the two select
    on different things and stop on different terms. That one is keyed to
    `PROFILE_EXTRACTOR_VERSION` and skips a recording whose profile is already current; these
    columns have no version of their own, and every candidate is re-read every run - which is
    cheap enough and is what makes it correct to run again after a reader fix without a
    version to bump.

    Idempotent, and safe to run repeatedly.

    **It no longer clears.** A logbook import that matches an existing recording and stores no
    bytes fills a blank `cns_end` from the document, and an outright rewrite here would delete
    that reading on the next run with nothing on the volume to recover it from. So a run fills
    only where a stored file yields a value and the column has none; what it can no longer do
    is *null* a reading it has decided is bogus, and that is the accepted cost.

    **It walks every recording** for what is the recording's own - its readouts, its device
    and its settings - and is the recovery for recordings stored before the readouts moved
    there: the migration copied the dive's onto the primary alone, reading no file, so every
    other recording's stay NULL until this runs. **The dive's fixes and the mixture fields
    stay the primary's**, since a second computer's positions and cylinder labels are its own;
    see `merge_mixture_fields` for why a dive whose cylinders have been edited is skipped
    rather than reconciled.
    """
    # Imported here rather than at module scope, matching `backfill_profiles`: the crud
    # module is not otherwise part of this module's dependency surface, and keeping the
    # import next to its one use says so.
    from ..crud.crud_dive_mixtures import get_mixtures_for_dive
    from .cache_invalidation import invalidate_dive_caches

    stmt = select(
        DiveRecording.id.label("recording_id"),
        DiveRecording.dive_id,
        DiveRecording.user_id,
        DiveRecording.ordinal,
        DiveRecording.start_time,
        DiveRecording.utc_offset_minutes,
    ).order_by(DiveRecording.dive_id, DiveRecording.ordinal)
    if parser_key is not None:
        # A recording is a candidate when any of its files is recorded under that format, which
        # is what "re-read the FITs" means for a recording holding two.
        stmt = stmt.where(
            select(DiveFile.id)
            .where(DiveFile.recording_id == DiveRecording.id, DiveFile.parser_key == parser_key)
            .exists()
        )
    if limit is not None:
        stmt = stmt.limit(limit)

    candidates = list(await db.execute(stmt))
    examined = recordings_updated = mixtures_updated = mixtures_skipped = failed = 0
    # Recordings written since the last commit, rather than a position in `candidates`. An
    # index-modulo test sits below several `continue`s, so a recording that failed on exactly
    # the boundary skipped that commit and left up to two batches riding on the next one.
    pending = 0
    touched_user_ids: set[int] = set()

    for row in candidates:
        examined += 1

        try:
            files = await load_recording_files(db, recording_id=row.recording_id)
        except blob_store.BlobMissingError:
            # The row is there and its file is not, which is data loss or an unmounted
            # volume rather than a race. Counted as a failure and the run continues, on the
            # same terms as the read failure below: a run that stopped here would report
            # less than one that finished and said how many recordings are in this state.
            logger.error("Skipping recording %s: a stored file is missing from the volume", row.recording_id)
            failed += 1
            continue
        if not files:
            # A recording with nothing to re-read - one a logbook import made from a document. Not a
            # failure and not an update: there is no file, so there is nothing this script
            # can say about it that is not already stored.
            continue

        # Synchronously, unlike every request path: this runs from a one-shot script whose
        # event loop has nothing else on it, so the threadpool hop would buy nothing and cost
        # a handoff per recording in the corpus.
        extraction = extract_recording(files, start_time=row.start_time, utc_offset_minutes=row.utc_offset_minutes)
        if extraction.unreadable:
            # A file that read at attach time and does not now is a reader regression, and
            # a run that reported only successes would hide it - the same reasoning as
            # `BackfillReport`'s five counts.
            logger.warning("Skipping recording %s: one of its stored exports could not be read", row.recording_id)
            failed += 1
            continue

        primary = row.ordinal == 0
        updates: list[tuple[int, dict[str, object]]] | None = []
        if primary:
            stored = await get_mixtures_for_dive(db, row.dive_id)
            updates = merge_mixture_fields(extraction.mixtures, stored)
            if updates is None:
                # `max`, not `len(stored)`: the count mismatch that refuses the merge includes
                # the diver having deleted every cylinder, and `len(stored)` is 0 there - so
                # the run reported "nothing skipped" for precisely the dive whose cylinders
                # were edited most. Whichever side has rows is what went unwritten.
                mixtures_skipped += max(len(stored), len(extraction.mixtures))

        if dry_run:
            recordings_updated += 1
            mixtures_updated += len(updates or [])
            continue

        source = extraction.device_source
        try:
            # A savepoint, so a recording the database rejects costs only that recording.
            # Without it the failure propagates out of this function and the enclosing
            # `async with local_session()` rolls back everything since the last batch commit
            # - and because nothing here advances a version column, the next run reaches the
            # same row and dies the same way.
            async with db.begin_nested():
                await dive_recordings.fill_readouts(db, recording_id=row.recording_id, readouts=extraction.scalars)
                await dive_recordings.fill_device_fields(
                    db, recording_id=row.recording_id, device=None if source is None else source.device
                )
                await dive_recordings.fill_recording_settings(
                    db,
                    recording_id=row.recording_id,
                    mode=None if source is None else source.mode,
                    deco_model=None if source is None else source.deco_model,
                    salinity=None if source is None else source.salinity,
                )
                if primary:
                    await fill_tech_scalars(db, dive_id=row.dive_id, scalars=extraction.scalars)
                    for mixture_id, values in updates or []:
                        await db.execute(update(DiveMixture).where(DiveMixture.id == mixture_id).values(**values))
        except IntegrityError:
            # A value the schema let through and the database won't take: a reader unit bug,
            # and the file that proves it is still attached to the dive. Counted rather than
            # raised, for the same reason as the read failure above.
            logger.warning(
                "Skipping recording %s: its parsed values violate a constraint", row.recording_id, exc_info=True
            )
            failed += 1
            continue

        recordings_updated += 1
        mixtures_updated += len(updates or [])
        touched_user_ids.add(row.user_id)

        pending += 1
        if pending >= _BACKFILL_BATCH_SIZE:
            await db.commit()
            pending = 0

    if not dry_run:
        await db.commit()
        # Cached dive reads embed the readouts, the fixes and the mixtures, so every dive this
        # run touched is now serving stale values. See the script for why this needs a live
        # Redis pool.
        for user_id in touched_user_ids:
            await invalidate_dive_caches(user_id)

    return TechBackfillReport(
        examined=examined,
        recordings_updated=recordings_updated,
        mixtures_updated=mixtures_updated,
        mixtures_skipped=mixtures_skipped,
        failed=failed,
    )
