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
files, and the two things derived from them (the profile, and the dive's oxygen-exposure
readings).

*A second file of one recording fills and never overwrites*, and that rule is written once per
thing it applies to - the device columns, the two match figures, the recording's start, the
dive's tech scalars, the profile's channels and the dive's cylinders. Derive them from
`git grep -n "def fill_" -- src/app/services` rather than from a count here, which is what
stops the list going stale; read it as a superset, since the cylinder one is implemented by
two pure helpers that match the same grep and are not themselves things the rule applies to.
Each takes every value from the *first* file that recorded it. The rejected alternative is
"the later file wins", which silently loses a value a diver corrected between two uploads.
"""

import hashlib
import logging
import uuid as uuid_pkg
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal

from fastapi import UploadFile
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from uuid6 import uuid7

from ..core.db.database import release_read_transaction
from ..core.security import verify_dive_file_token
from ..core.utils.datetime_offset import split_local_start_time
from ..core.utils.uploads import read_upload_within_limit, safe_filename
from ..models.dive import Dive
from ..models.dive_file import DiveFile
from ..models.dive_mixture import DiveMixture
from ..models.dive_recording import DiveRecording
from ..schemas.dive import DiveFileInfo, DiveTechScalars
from ..schemas.dive_mixture import DiveMixtureCreate, DiveMixtureRead, as_create
from ..schemas.parsed_dive import DiveMixtureSchema, ParsedDiveSchema
from . import blob_store, dive_recordings
from .dive_parsers import PARSER_BY_KEY, DiveParseError, DiveParser, UnsupportedDiveFileError
from .dive_profiles import (
    NormalizedProfile,
    attribute_and_cap,
    delete_profile_for_recording,
    delete_profiles_for_dive,
    fill_channels,
    get_existing_profile,
    normalize,
    recording_source_digest,
    should_extract,
    store_profile,
)

logger = logging.getLogger(__name__)

# The key prefix every stored export is written under - see `blob_store.new_key`.
KEY_KIND = "dive-files"

# Matches the cap `/dive/parse` reads under, since the same file makes both trips: a
# limit here that was lower would let a file pre-fill a form and then be refused
# storage. Exports are small (a few hundred KB); this is headroom, not a target.
MAX_DIVE_FILE_SIZE = 5 * 1024 * 1024  # 5 MB

# The `dive` columns an import owns outright. Taken from `DiveTechScalars` rather than
# listed here, so the schema that publishes them and the write that fills them cannot
# drift: adding a field to one is adding it to both.
TECH_SCALAR_FIELDS = tuple(DiveTechScalars.model_fields)


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
    recording's profile is a function of its files' digests in order, and re-reading a file
    means finding the parser it was read by rather than sniffing it again.
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


def extract_tech_scalars(parser: type[DiveParser], content: bytes) -> dict[str, float | None] | None:
    """The dive's oxygen-exposure and surface-pressure readings, or `None` if unreadable.

    **Never raises**, on the same terms and for the same reason as `extract_profile`: the
    file is the durable artifact, so a header this build can't read must not fail the
    upload that would have preserved it for a later fix. The dive simply keeps whatever
    it had until a backfill run picks it up.

    Re-parses rather than reusing what `POST /dive/parse` already produced. That parse
    happened in a different request, and the only thing carried forward from it is a
    signature over the *content hash* - so the alternative would be trusting a client to
    hand back the numbers it was shown, for columns the form is deliberately not allowed
    to write. The parse costs a few hundred milliseconds on the thread that is already
    extracting the profile.

    Returns a dict rather than a schema because it is spread straight into an `UPDATE`;
    an all-`None` result is still returned, so the caller can decide between the outright
    write (which clears what nothing yields) and the fill (which does not).

    **On the attach path this is now the fallback, not the normal route.** `_extract_all`
    goes through `parser.parse_all`, which decodes once and returns both halves; pairing
    this function with `extract_profile` instead makes FIT call `FitParser._scan` twice,
    measured at 55 ms + 58 ms on a 26 KB export where the single scan serving both is
    58 ms, and scaling with `_MAX_FRAMES` up to the ~1.5 s the profile extraction is
    budgeted at. The two Suunto parsers are cheap enough for it not to matter.

    That second pass is what buys the property the fallback exists for: this function and
    `extract_profile` fail *independently*, so a file whose samples are malformed still
    yields its header scalars, and vice versa. Paying a redundant decode on a file that
    was already failing is the right side of that trade.
    """
    try:
        parsed = parser.parse(content)
    except DiveParseError, UnsupportedDiveFileError:
        logger.warning("Tech-scalar extraction failed for a %s file: unreadable header", parser.key, exc_info=True)
        return None
    except Exception:
        logger.exception("Unexpected error extracting tech scalars from a %s file", parser.key)
        return None
    return {name: getattr(parsed, name) for name in TECH_SCALAR_FIELDS}


@dataclass(frozen=True, slots=True)
class FileExtraction:
    """Everything one file yields: its header, its samples, and the failure of either.

    `parsed` is `None` when the header could not be read at all, which is a different fact
    from a header that read and said nothing - the first leaves the recording's device
    columns untouched, the second is a device the file did not name.

    **`profile` is normalized and nothing more** - not attributed, not capped. Those two
    steps belong to the recording rather than to the file: `extract_recording` fills the
    channels across a recording's files and then runs them *once*, which is the only order
    under which the gas attribution is computed against the channels it will be stored beside
    and at the resolution `derive_gas_attribution` requires. `NormalizedProfile.duration` is
    the same either way (`downsample` keeps each channel's first and last sample), so the
    caller reading a sampled span off this gets the same number it always did.
    """

    parsed: ParsedDiveSchema | None
    profile: NormalizedProfile | None
    scalars: dict[str, float | None] | None


def _extract_all(parser: type[DiveParser], content: bytes) -> FileExtraction:
    """Both extractions over one set of bytes, for one `run_in_threadpool` hop.

    They are separate functions because they answer separate questions and are tested
    separately, but they are always wanted together and both are pure CPU - so pairing
    them here keeps the attach path to a single thread handoff instead of two, and keeps
    the "released the read transaction first" reasoning applying to one call.

    Goes through `parse_all` so a parser that can do both off one decode does: FIT
    otherwise scans the file twice, at roughly double the CPU of the single pass it
    needs. On any failure it falls back to the two independent extractions, which is
    what preserves their most useful property - a file whose *samples* are malformed
    still yields its header scalars, and vice versa. The fallback re-decodes, and that
    is the right trade: it costs a second pass only on a file that was already failing,
    where nothing about the latency budget matters any more.
    """
    try:
        parsed, profile = parser.parse_all(content)
    except Exception:
        # Deliberately bare: `parse_all` promises the two parser exceptions, but the
        # fallback is correct for anything at all and swallowing more here costs nothing
        # - `extract_profile` and `extract_tech_scalars` do their own logging, with the
        # per-half message that says which of the two actually went wrong.
        return FileExtraction(
            parsed=_parse_header(parser, content),
            profile=_normalized_profile(parser, content),
            scalars=extract_tech_scalars(parser, content),
        )

    try:
        scalars: dict[str, float | None] | None = {name: getattr(parsed, name) for name in TECH_SCALAR_FIELDS}
    except AttributeError:
        logger.exception("Unexpected error extracting tech scalars from a %s file", parser.key)
        scalars = None

    try:
        return FileExtraction(
            parsed=parsed, profile=normalize(profile) if profile is not None else None, scalars=scalars
        )
    except Exception:
        logger.exception("Unexpected error extracting a profile from a %s file", parser.key)
        return FileExtraction(parsed=parsed, profile=None, scalars=scalars)


def _normalized_profile(parser: type[DiveParser], content: bytes) -> NormalizedProfile | None:
    """One file's samples, normalized and no further, never raising.

    `extract_profile`'s contract with `extract_profile`'s two steps removed - see
    `FileExtraction.profile` for why the attribution and the cap belong to the recording.
    """
    try:
        parsed = parser.parse_profile(content)
    except DiveParseError:
        logger.warning("Profile extraction failed for a %s file: malformed samples", parser.key, exc_info=True)
        return None
    except Exception:
        logger.exception("Unexpected error extracting a profile from a %s file", parser.key)
        return None
    return normalize(parsed) if parsed is not None else None


def _parse_header(parser: type[DiveParser], content: bytes) -> ParsedDiveSchema | None:
    """The header alone, never raising, for the fallback path above."""
    try:
        return parser.parse(content)
    except DiveParseError, UnsupportedDiveFileError:
        logger.warning("Header extraction failed for a %s file", parser.key, exc_info=True)
        return None
    except Exception:
        logger.exception("Unexpected error reading a %s file's header", parser.key)
        return None


@dataclass(frozen=True, slots=True)
class RecordingExtraction:
    """What a recording's files say, read in attach order under the fill rule.

    One object rather than four returns because they are read together everywhere and every
    one of them is *the first file that recorded it* - only the unit differs. The profile
    takes each channel whole from the earliest file carrying it; the scalars take each
    reading likewise; the device likewise; and the cylinders take each *member* of each
    cylinder likewise, which is one level finer because a Suunto's two exports of one dive
    split the pressures and the gas fraction between them. `unreadable` says at least one
    file could not be re-read at all, which is a parser regression the backfill counts rather
    than swallows.
    """

    profile: NormalizedProfile | None = None
    scalars: dict[str, float | None] = field(default_factory=dict)
    device_source: ParsedDiveSchema | None = None
    mixtures: list[DiveMixtureSchema] = field(default_factory=list)
    unreadable: bool = False


def extract_recording(
    files: Sequence[LoadedDiveFile], known: Mapping[str, FileExtraction] | None = None
) -> RecordingExtraction:
    """Read a recording's files in order and fill each answer from the first that carries it.

    Pure and DB-free, on this module's `reconcile` idiom: the fill rule is the decision worth
    testing, and it is testable with a list of bytes and no database at all. **Pure CPU, and
    the whole of it** - every caller on a request path hands it to `run_in_threadpool`, for
    the reason *"Uploaded files are parsed in a thread, not on the event loop"* in
    `DECISIONS.md` gives: a FIT is up to ~1.7 s of Python at `_MAX_FRAMES`.

    **Order is attach order** and the caller guarantees it (`ORDER BY dive_file.id`), because
    "the first file that recorded it" is meaningless without one. A file whose parser this
    build no longer has, or which stopped parsing, sets `unreadable` and contributes nothing -
    it is not silently treated as a file that said nothing, because those two facts lead to
    opposite repairs.

    `known` maps a digest to an extraction the caller already has, and exists for exactly one
    caller: the attach path has just decoded the incoming file to decide which recording it
    belongs to, and would otherwise decode it a second time here - the double decode
    `parse_all` was introduced to remove, reintroduced one level up.
    """
    result = RecordingExtraction()
    for file in files:
        extraction = (known or {}).get(file.sha256)
        if extraction is None:
            parser = PARSER_BY_KEY.get(file.parser_key)
            if parser is None:
                logger.warning("A stored file is recorded under a parser key this build lacks: %r", file.parser_key)
                result = replace(result, unreadable=True)
                continue
            extraction = _extract_all(parser, file.data)
        if extraction.parsed is None and extraction.profile is None and extraction.scalars is None:
            result = replace(result, unreadable=True)
            continue

        result = RecordingExtraction(
            profile=fill_channels(result.profile, extraction.profile),
            # `|` with the stored side second is the fill: a key already carrying a value
            # keeps it, and one carrying `None` is overwritten by a later file's reading.
            scalars={
                name: result.scalars.get(name) if result.scalars.get(name) is not None else value
                for name, value in (extraction.scalars or {}).items()
            }
            or result.scalars,
            device_source=result.device_source or extraction.parsed,
            # Per member, not per list - `fill_parsed_mixtures` says why the whole-list
            # reading drops the one gas fraction the corpus pair records.
            mixtures=fill_parsed_mixtures(
                result.mixtures, [] if extraction.parsed is None else extraction.parsed.mixtures
            ),
            unreadable=result.unreadable,
        )

    # Attributed and capped **once, here**, over the filled channels - which is why every
    # `FileExtraction.profile` above is normalized and nothing more. Attribution reads the gas
    # switches back against the depth channel and after a fill those two may have come from
    # different files, so it has to be derived from the merged result; and it has to be derived
    # *before* the cap, because `downsample` keeps each bucket's extremes and throws the rest
    # away. `attribute_and_cap` is the same two steps `finalize_profile` runs, in the same
    # order, which is what stops a recording's profile and a single file's ever disagreeing.
    return replace(result, profile=attribute_and_cap(result.profile))


def extract_recording_profile(files: Sequence[LoadedDiveFile]) -> tuple[NormalizedProfile | None, bool]:
    """`extract_recording`'s profile half, for `backfill_profiles`, plus its `unreadable` flag."""
    extraction = extract_recording(files)
    return extraction.profile, extraction.unreadable


async def store_tech_scalars(
    db: AsyncSession, *, dive_id: int, scalars: dict[str, float | None], commit: bool = False
) -> None:
    """Write a dive's parsed tech scalars **outright**, in the caller's transaction.

    Outright means every field of `DiveTechScalars`, `None` included - so a re-derivation
    that no longer yields a reading clears the one that is there rather than stranding a
    number nothing can re-derive. Used where the recording's whole set of files has just
    been read and the dive had nothing on it to lose: the file that *created* a primary
    recording, and every re-derivation after a deletion or a promotion. Not a primary
    recording's first file as such - see `_rederive_recording` on what `fresh` means.

    `commit=False` by default for the same reason as `store_profile`: the attach path writes
    the file, the profile and these in one transaction, so a dive can never end up
    describing a recording it does not have.
    """
    await db.execute(update(Dive).where(Dive.id == dive_id).values(**scalars))
    if commit:
        await db.commit()


async def fill_tech_scalars(db: AsyncSession, *, dive_id: int, scalars: dict[str, float | None]) -> None:
    """Write only the readings the dive does not already have. **Never overwrites.**

    The tech-scalar half of *a second file of one recording fills and never overwrites*: a
    FIT arriving beside a JSON of one dive contributes the `cns_end` and `otu_end` the JSON
    had none of, and leaves the positions the JSON supplied exactly as they are.

    A `COALESCE` per column rather than read-then-write, for `fill_device_fields`' reason:
    one statement, no read to race, and the rule stated once in SQL instead of once in SQL
    and once in Python.
    """
    values = {name: func.coalesce(getattr(Dive, name), value) for name, value in scalars.items() if value is not None}
    if values:
        await db.execute(update(Dive).where(Dive.id == dive_id).values(**values))


def relabel_gas_numbers[MixtureRow: (DiveMixtureSchema, DiveMixtureRead)](
    parsed: Sequence[MixtureRow], stored: Sequence[DiveMixtureRead]
) -> tuple[dict[int, int], list[MixtureRow]]:
    """Map a second computer's cylinder labels onto the dive's own list.

    Returns `(old gas_number -> the dive's gas_number, mixtures to append)`.

    **Generic over the incoming row, and the appended rows come back as whatever went in.**
    The two shapes this is asked about are a *file's* cylinders (`DiveMixtureSchema`, the
    attach and import paths) and a *dive's* stored ones (`DiveMixtureRead`, which is what
    the other half of a merge holds). Narrowing the parameter to the parsed shape would make
    a merge convert its stored rows to it first, and that conversion silently drops `usage` -
    a member no format records and only a diver can have typed, which is exactly the value
    that must survive the cylinder being carried onto another dive. The join reads only
    `oxygen`, `helium` and `gas_number`, which both shapes carry and mean the same thing by.

    **`gas_number` is dive-scoped, and that is the ruling this implements.** The app derives
    gas consumption from the diver's editable cylinders joined to a profile's pressure
    channels and gas-switch events by that label (see *"The cylinder pressures come from the
    mixtures"* in `DECISIONS.md`), so a second computer whose own labelling calls the deco
    bottle `1` would otherwise attribute its pressures to the dive's back gas. Subsurface
    renumbers a second computer's sensors onto the dive's cylinder list for the same reason.

    **By mix first, then by order, and only then appended.** The mix is the part of a
    cylinder a diver has no reason to retype and every reason to leave alone, so two rows
    agreeing on `(oxygen, helium)` are the same tank; position is the weaker fallback, kept
    because a pair of air cylinders records no distinguishing mix at all; and a cylinder the
    dive's list does not have is a real one the second computer saw, appended with the next
    free label rather than dropped.

    Pure and DB-free, beside `fill_mixture_fields` and `merge_mixture_fields` below - and
    deliberately neither of them. Those two join a *second reading of the same recording* to
    the dive's rows by position and ask what may be written into them; this one joins a
    *different computer's* cylinder list to the dive's by mix, and writes nothing at all -
    its answer is a renumbering. It also always answers, where both of those can refuse.
    """
    remaining = list(stored)
    mapping: dict[int, int] = {}
    unmatched: list[MixtureRow] = []

    def claim(row: DiveMixtureRead, incoming: MixtureRow) -> None:
        remaining.remove(row)
        if incoming.gas_number is not None and row.gas_number is not None:
            mapping[incoming.gas_number] = row.gas_number

    by_position: list[MixtureRow] = []
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
        claim(match, incoming)

    for incoming in by_position:
        if not remaining:
            unmatched.append(incoming)
            continue
        claim(remaining[0], incoming)

    taken = {row.gas_number for row in stored if row.gas_number is not None}
    taken |= set(mapping.values())
    next_free = max(taken, default=0) + 1
    appended: list[MixtureRow] = []
    for incoming in unmatched:
        if incoming.gas_number is not None:
            mapping[incoming.gas_number] = next_free
        appended.append(incoming.model_copy(update={"gas_number": next_free}))
        next_free += 1

    return mapping, appended


def apply_gas_mapping(profile: NormalizedProfile | None, mapping: dict[int, int]) -> NormalizedProfile | None:
    """Rewrite a profile's cylinder labels through `relabel_gas_numbers`' map.

    Both places a `gas_number` appears in the stored payload - the pressure channels and the
    `gas_switch` events - because a map applied to one and not the other would leave a dive
    whose switches name a tank its pressure curves do not.

    A label the map does not mention is left alone. That is the identity case (a primary
    recording, whose labels are already the dive's) and it is also the honest answer for a
    channel whose number the mapping could not place.
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
    )


# What a second reading of one recording may put into a cylinder the stored row left blank.
# `po2_limit`, `role`, `gas_number` and `usage` are deliberately not here, and each for its own
# reason: `usage` is a distinction no format records at all, `po2_limit` and `role` are the
# diver's plan rather than the tank's contents, and `gas_number` is the join key to the stored
# profile's pressure channels - a label taken from a second file would rename a cylinder whose
# curves are already attributed under the first file's numbering.
FILLABLE_MIXTURE_FIELDS = ("oxygen", "helium", "volume", "start_pressure", "end_pressure")


def fill_mixture_fields(
    parsed: Sequence[DiveMixtureSchema], stored: Sequence[DiveMixtureSchema | DiveMixtureRead]
) -> list[dict[str, float]] | None:
    """What a second reading of one recording may add to cylinders that already exist.

    One dict per `stored` row **in order**, so the result indexes alongside the list it was
    given; `None` when the two lists can't be shown to describe the same cylinders. An empty
    dict means "this row is complete, or the file said nothing it lacks", which is the
    ordinary answer and not a failure. Pure and DB-free, on this module's `reconcile` idiom.

    **This is not `merge_mixture_fields`, and the two answer opposite questions.** That one
    is the backfill's: may these parsed values be written *over* these stored rows? It writes
    `po2_limit`, `gas_number` and `role`, and overwrites them. This one is the fill rule's
    cylinder half - it writes only where the stored row has nothing, and only the five members
    above, so a value the diver typed or an earlier file recorded can never be replaced. They
    share the join and nothing else, which is why this is a second function rather than a flag
    on that one.

    **The join is positional, on `merge_mixture_fields`' terms and with its preconditions.**
    Mixtures are replaced wholesale on every save, so a stored row's `id` postdates the file
    and says nothing about which parsed cylinder it came from; `stored` must therefore be in
    saved order, which `get_mixtures_for_dive`'s `ORDER BY id` guarantees and nothing here can
    check. The counts must match and every pair must still agree on the `(oxygen, helium)`
    both sides recorded - a `None` on either side being an absent reading rather than a
    disagreement. A list that half-matches is an edited list describing other cylinders.

    **A fill the table would reject is dropped, and only for the row it belongs to.** All of
    these but `volume` are half of a pair the schema constrains - `end_pressure <=
    start_pressure`, `oxygen + helium <= 100` - so filling one half against a stored other
    half can produce a row the database refuses, and an `IntegrityError` here would surface to
    the diver as a failed upload or take a whole logbook import down with it. Where that
    happens the file and the stored row cannot both be describing this cylinder, and the
    stored row is the diver's: it stands, whole. Per row rather than per dive, unlike
    `merge_mixture_fields`' refusal, because nothing is being overwritten - a filled cylinder
    beside an unfilled one is two rows each carrying what it always did, not two rows sourced
    from different places.
    """
    if len(parsed) != len(stored) or not parsed:
        return None

    filled: list[dict[str, float]] = []
    for parsed_mix, stored_mix in zip(parsed, stored, strict=True):
        if parsed_mix.oxygen is not None and stored_mix.oxygen is not None and parsed_mix.oxygen != stored_mix.oxygen:
            return None
        if parsed_mix.helium is not None and stored_mix.helium is not None and parsed_mix.helium != stored_mix.helium:
            return None

        values: dict[str, float] = {}
        for name in FILLABLE_MIXTURE_FIELDS:
            reading = getattr(parsed_mix, name)
            if reading is not None and getattr(stored_mix, name) is None:
                values[name] = reading
        filled.append(values if _fill_is_storable(stored_mix, values) else {})
    return filled


def _fill_is_storable(stored_mix: DiveMixtureSchema | DiveMixtureRead, values: dict[str, float]) -> bool:
    """Would the row `values` produces still satisfy `dive_mixture`'s own constraints?

    The table's `CHECK`s over these five columns, restated here because this is the one write
    path that can compose a row out of two sources: everything else that reaches these columns
    arrives as a whole cylinder from one place, already validated by `DiveMixtureCreate` or by
    the parse-side bounds on `DiveMixtureSchema`. A fill mixes them, and neither of those
    layers sees the combination.

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


def fill_parsed_mixtures(
    earlier: Sequence[DiveMixtureSchema], later: Sequence[DiveMixtureSchema]
) -> list[DiveMixtureSchema]:
    """A recording's cylinders once a later file of it has been read.

    The mixture half of `extract_recording`'s "each value from the first file that recorded
    it", and it has to be per *member* rather than per list: the corpus pair is a Suunto Ocean
    JSON whose cylinder carries pressures and no gas fraction beside the same computer's FIT,
    which carries `oxygen` 33 and no pressures. Taking the first list whole lands one of the
    two and drops the other, which leaves the dive's only cylinder without the mix its own
    computer recorded.

    A later file whose cylinders cannot be joined to the earlier ones contributes nothing,
    rather than replacing them: the earlier list is what the recording's stored profile is
    already labelled against.
    """
    if not earlier:
        return list(later)
    values = fill_mixture_fields(later, earlier)
    if values is None:
        return list(earlier)
    return [row.model_copy(update=fill) if fill else row for row, fill in zip(earlier, values, strict=True)]


async def fill_dive_mixtures(db: AsyncSession, *, dive_id: int, parsed: Sequence[DiveMixtureSchema]) -> None:
    """Write `fill_mixture_fields`' answer onto the dive's own cylinders.

    Shared by the two paths a second reading of one recording arrives on - a file attached to
    a recording that already has one, and a logbook import matching an incoming recording to a
    stored one - so what a fill writes is stated once. **Whether one runs is still each
    caller's**, and the two answer it differently. `_rederive_recording` skips this twice
    over: for a secondary recording, and - the gate that matters here - unless `joined` says
    new bytes arrived on a recording that already existed, which is false for a re-upload of
    files the recording already had and for a deletion. The import writer has no ordinal to
    hand and calls it for whichever recording matched, as it already does for
    `fill_tech_scalars`. In the caller's transaction, like every other `fill_`: the file's
    row, the profile and this land together or not at all.

    Read-then-write rather than a `COALESCE` per column, unlike `fill_tech_scalars`, because
    the join is positional and the alignment is only visible to Python - the statement would
    have to name a row the SQL cannot pick out. There is no race to lose: every caller is
    inside a transaction on a dive only its owner can reach.
    """
    from ..crud.crud_dive_mixtures import get_mixtures_for_dive

    if not parsed:
        return
    stored = await get_mixtures_for_dive(db=db, dive_id=dive_id)
    values = fill_mixture_fields(parsed, stored)
    if values is None:
        return
    for row, fill in zip(stored, values, strict=True):
        if fill:
            await db.execute(update(DiveMixture).where(DiveMixture.id == row.id).values(**fill))


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
    oversized, `InvalidDiveFileTokenError` if it isn't accompanied by a valid parse
    receipt for these exact bytes, and `DiveFileAlreadyLinkedError` if the same content
    is already stored against another dive.

    The token is the admission control, unchanged. Re-running the parser registry here would
    only establish that the bytes *look* parseable, which would let this endpoint store any
    blob shaped like an export and would not tie the stored file to the parse that pre-filled
    the dive's form. Checking a signature over the content hash establishes both, and costs
    one HMAC over a digest the dedupe needs anyway.

    **Where the file lands is the same-recording test's answer**, applied within this dive
    only - the caller has already said which dive these bytes belong to, so the question left
    is which of *its* records they are a second reading of. A match fills that recording's
    blanks and never overwrites them; no match appends a new recording after the last.
    """
    data = await read_upload_within_limit(upload, MAX_DIVE_FILE_SIZE)
    digest = hashlib.sha256(data).hexdigest()
    parser = _admit(user_uuid=user_uuid, digest=digest, file_token=file_token)

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

    # Deliberately *before* the transaction below, not inside it: an exception raised in
    # there is caught by the `IntegrityError` handler and reported to the diver as a
    # concurrent-upload conflict, which a parse failure is not.
    #
    # In a thread for the same reason `POST /dive/parse` parses in one: sampling a FIT file
    # is pure Python and takes up to ~1.5 s at `_MAX_FRAMES`, and this is an `async def`.
    # The read transaction is released first so the connection isn't held idle for the
    # duration - see `release_read_transaction`.
    await release_read_transaction(db)
    extraction = await run_in_threadpool(_extract_all, parser, data)

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
    # join it - **both read and parsed before the transaction opens**, so the write below is
    # writes only. `known` hands over the extraction of the incoming file, which was decoded
    # a few lines up to decide where it lands: without it this would decode it a second time,
    # which is the double decode `parse_all` exists to remove.
    filename = safe_filename(upload.filename, default="dive-file")
    now = datetime.now(UTC)
    incoming_file = LoadedDiveFile(
        data=data,
        content_type=parser.content_type,
        original_filename=filename,
        sha256=digest,
        parser_key=parser.key,
    )
    existing_files = [] if matched is None else await load_recording_files(db, recording_id=matched.id)
    # Appended last, which is where `ORDER BY dive_file.id` will put it once the row lands -
    # and attach order is the whole of what "the first file that recorded it" means.
    files = [*existing_files, incoming_file]
    # Released a *second* time, because the two reads above reopened a transaction the first
    # release had closed and nothing has been written yet. Without this the connection sits
    # idle-in-transaction across the parse of every file already on the recording and the blob
    # write below - which is the pool exhaustion `release_read_transaction` exists to prevent,
    # arrived at from the other side. Safe for the reason that function documents: everything
    # this still needs (`matched`, `files`) is a frozen dataclass, detached from the session.
    await release_read_transaction(db)
    recording_extraction = await run_in_threadpool(extract_recording, files, {digest: extraction})
    # The file lands on the volume *before* the transaction that references it. Every
    # database-visible state therefore names bytes that exist; the only thing a crash
    # between the two can produce is an unreferenced file, which is harmless until the
    # sweeper reclaims it. The key carries a nonce minted per write - see
    # `blob_store.new_key`.
    storage_key = blob_store.new_key(KEY_KIND, sha256=digest)
    await blob_store.put(storage_key, data)

    try:
        if matched is not None:
            recording_id, ordinal = matched.id, matched.ordinal
            await dive_recordings.fill_device_fields(
                db, recording_id=recording_id, device=None if extraction.parsed is None else extraction.parsed.device
            )
            if incoming is not None:
                await dive_recordings.fill_gate_figures(
                    db, recording_id=recording_id, duration=incoming.duration, max_depth=incoming.max_depth
                )
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
                content_type=parser.content_type,
                byte_size=len(data),
                original_filename=filename,
                parser_key=parser.key,
                storage_key=storage_key,
                # Spelled out rather than left to `PublicUUIDMixin`'s `default_factory`:
                # that is a dataclass-level default applied when the ORM constructs an
                # instance, and this Core-level INSERT never constructs one.
                uuid=file_uuid,
                created_at=now,
            )
        )
        await _rederive_recording(
            db,
            recording_id=recording_id,
            dive_id=dive_id,
            ordinal=ordinal,
            fresh=matched is None,
            joined=matched is not None,
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


def _admit(*, user_uuid: uuid_pkg.UUID, digest: str, file_token: str) -> type[DiveParser]:
    """The token checks, in one place. Returns the parser the receipt names."""
    claims = verify_dive_file_token(file_token)
    if claims is None:
        raise InvalidDiveFileTokenError("This import has expired. Re-import the file to attach it.")
    if claims.user_uuid != str(user_uuid):
        raise InvalidDiveFileTokenError("This import belongs to a different account. Re-import the file to attach it.")
    if claims.sha256 != digest:
        raise InvalidDiveFileTokenError("This file doesn't match the one that was imported. Re-import it to attach it.")

    # A `parser_key` this build doesn't know means the token outlived a parser being
    # renamed or removed, and there is nothing to record the file as.
    parser = PARSER_BY_KEY.get(claims.parser_key)
    if parser is None:
        raise InvalidDiveFileTokenError("This import is no longer supported. Re-import the file to attach it.")
    return parser


def _incoming_facts(extraction: FileExtraction) -> dive_recordings.RecordingFacts | None:
    """The gates' view of a file just parsed, or `None` when it named no start.

    **`duration` and `max_depth` here are the device's own logged figures**, off the header,
    not the samples' - which is what the attach path stores and what makes the column's
    meaning "a duration the gate can compare" rather than one number with one meaning. The
    sampled span rides along separately, because the same-recording gate compares that and
    the strict gate does not.

    A file that recorded no start time cannot be matched or placed on the clock at all, so it
    gets a recording of its own with a NULL start - which is what a header-only export with
    no timestamp is.
    """
    parsed = extraction.parsed
    if parsed is None or parsed.start_time is None:
        return None
    try:
        start_time, offset_minutes = split_local_start_time(datetime.fromisoformat(parsed.start_time))
    except ValueError:
        logger.warning("A parsed start time was not a datetime this app can store: %r", parsed.start_time)
        return None
    return dive_recordings.RecordingFacts(
        device=dive_recordings.device_of(parsed.device),
        start_time=start_time,
        utc_offset_minutes=offset_minutes,
        duration=parsed.duration,
        max_depth=parsed.max_depth,
        sampled_span=None if extraction.profile is None else extraction.profile.duration,
    )


async def read_recording(
    db: AsyncSession,
    *,
    recording_id: int,
    known: Mapping[str, FileExtraction] | None = None,
    release: bool = False,
) -> tuple[list[LoadedDiveFile], RecordingExtraction]:
    """A recording's files and what they say, with the parsing done **off the event loop**.

    The seam that keeps `extract_recording` - pure CPU, up to ~1.7 s of it per FIT - out of
    the request's own thread, which is what *"Uploaded files are parsed in a thread, not on
    the event loop"* in `DECISIONS.md` requires.

    **`release` says the caller has nothing left in the transaction to lose**, and it has to be
    the caller's answer rather than this function's. `release_read_transaction` rolls back, so
    it can free the connection for the length of the parse only where nothing has been written
    yet - which is true of `_repeat_upload`, whose reads are all lookups, and false of
    `refresh_tech_scalars`, which every caller reaches after a delete or a promotion has
    already been issued. There the connection is held for the duration, and that is the
    accepted cost: what is bought either way is the event loop, which is the scarce thing.
    """
    files = await load_recording_files(db, recording_id=recording_id)
    if not files:
        return [], RecordingExtraction()
    if release:
        await release_read_transaction(db)
    return files, await run_in_threadpool(extract_recording, files, known)


async def _rederive_recording(
    db: AsyncSession,
    *,
    recording_id: int,
    dive_id: int,
    ordinal: int,
    fresh: bool,
    joined: bool,
    files: Sequence[LoadedDiveFile],
    extraction: RecordingExtraction,
) -> None:
    """Rewrite everything derived from one recording's files. **Writes only.**

    Called after any change to a recording's files, with the files and their extraction from
    `read_recording` - which is what keeps the parsing off the event loop and out of this
    function entirely. Deriving from *all* of them, rather than folding the new one into what
    is stored, is what makes the fill rule mean the same thing on every path: the answer is a
    function of the files in attach order and of nothing else, so an attach, a deletion and a
    backfill run cannot disagree about it.

    **The dive's tech scalars are the primary recording's, and only the primary's.** A
    secondary recording is a second computer's account of the same dive; its CNS clock is its
    own device's and writing it onto the dive would attribute one computer's arithmetic to
    another's. **`fresh` says the dive had nothing on this recording to lose** - the caller's
    answer, not a property of the row - which is what decides between the outright write
    (clearing what nothing yields) and the fill. The attach path sets it from
    `matched is None`, so it is true of a recording this very upload created and **false of a
    file-less one a logbook import created earlier**: that recording's first file is a second
    reading of a record the logbook already holds, and fills. It is deliberately *not* "had no
    files a moment ago" - `delete_dive_file` passes `fresh=True` for a recording that plainly
    did, because there the point is to stop claiming a reading the remaining files no longer
    yield.

    **A secondary recording's cylinder labels are mapped onto the dive's**, which is the other
    half of that asymmetry: its samples stay, and only the numbers naming which tank they
    came out of move. The *primary* recording's cylinders are joined to the dive's rows
    positionally instead and fill their blanks - the same first-file-wins rule the scalars
    follow one paragraph up.

    **`joined` and not `fresh` is what gates that, and the two are different questions.**
    `fresh` asks whether the dive has anything on this recording to lose; `joined` asks
    whether *new bytes* arrived on a recording that already existed, which is the only event
    that can put a reading into a cylinder. **On the attach path each is the other's
    negation**, which is why one parameter looked sufficient - and `_repeat_upload` is where
    that breaks, being the one caller that answers *both* with `False`: it re-reads bytes the
    recording already had, so it fills the scalars (a re-parse yielding less must not clear
    them) while nothing has arrived that could fill a cylinder. Deriving the cylinders from
    `fresh` there put back a value the diver had cleared, read straight off the very file they
    were editing away from. `delete_dive_file` has no new bytes either, and answers
    `(True, False)`. Two parameters rather than one because conflating them is the bug: a
    caller reasoning only about the scalars gets the cylinders wrong for free. Required rather
    than defaulted, so a fourth caller has to answer it.
    """
    from ..crud.crud_dive_mixtures import get_mixtures_for_dive, replace_mixtures_for_dive

    if not files:
        await delete_profile_for_recording(db, recording_id=recording_id, commit=False)
        return

    profile = extraction.profile

    if ordinal != 0 and extraction.mixtures:
        stored = await get_mixtures_for_dive(db=db, dive_id=dive_id)
        mapping, appended = relabel_gas_numbers(extraction.mixtures, stored)
        profile = apply_gas_mapping(profile, mapping)
        if appended:
            await replace_mixtures_for_dive(
                db=db,
                dive_id=dive_id,
                mixtures=[
                    *(as_create(row) for row in stored),
                    # `appended` is the parser's own shape, not a stored row - its `role`/
                    # `usage` are enums already, so it needs no `as_create`.
                    *(DiveMixtureCreate(**row.model_dump()) for row in appended),
                ],
                commit=False,
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
            commit=False,
        )

    if ordinal != 0:
        return
    if fresh:
        await store_tech_scalars(
            db,
            dive_id=dive_id,
            scalars={name: extraction.scalars.get(name) for name in TECH_SCALAR_FIELDS},
            commit=False,
        )
    else:
        await fill_tech_scalars(db, dive_id=dive_id, scalars=extraction.scalars)

    # Its own branch, not the `fresh` one - see the docstring for the two callers where the
    # answers differ. New bytes on a recording that already existed is the whole of the case:
    # the FIT's `oxygen` 33 landing in the cylinder the JSON gave pressures and no mix.
    if joined:
        await fill_dive_mixtures(db, dive_id=dive_id, parsed=extraction.mixtures)


async def refresh_tech_scalars(db: AsyncSession, *, dive_id: int, touched_primary: bool) -> None:
    """Rewrite a dive's tech scalars from its primary recording's files, outright.

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
    converted logbook import creates, the extraction is empty and every field is written
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
    await store_tech_scalars(
        db, dive_id=dive_id, scalars={name: scalars.get(name) for name in TECH_SCALAR_FIELDS}, commit=False
    )


async def _repeat_upload(
    db: AsyncSession, *, existing: _ExistingRow, data: bytes, dive_id: int, user_id: int
) -> StoredRecordingFile:
    """The same bytes, already stored against this dive. Idempotent, with two repairs.

    Nothing is rewritten, not even `original_filename`: the bytes are the file's identity,
    and re-uploading them is the client repeating itself.

    The *profile*, though, is a function of (these bytes, the extractor version), so a
    repeated attach after `PROFILE_EXTRACTOR_VERSION` was bumped opportunistically upgrades
    it from bytes already in hand. The tech scalars ride that same version gate, having none
    of their own, which makes a scalar-only parser fix invisible here - `backfill_tech_fields`
    is what picks those up.

    And re-uploading is the natural repair after a partial loss of the blob store, on
    either backend: without the `has`/`put` below the row says "already stored", the
    download 500s forever, and the server refuses the very bytes that would fix it. The
    write re-`put`s the key the row already carries rather than minting one, and a `put` of
    a key whose name ends in these bytes' hash is byte-identical to what was there.

    **The blob repair runs first, before anything reads the recording's files.** It has to:
    re-deriving a recording loads every file it holds, and `load_recording_files` raises
    `BlobMissingError` on bytes that are gone - so repairing second would make this path fail
    on exactly the condition it exists to fix. That ordering did not matter while a dive had
    one file and the re-extraction read the bytes it had just been handed; it matters now,
    and nothing but a test would have caught it.
    """
    if not await blob_store.has(existing.storage_key):
        logger.warning("Rewriting the missing stored file for dive %s from a re-upload", dive_id)
        await blob_store.put(existing.storage_key, data)

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
        # `release=True`: nothing above this has written anything - the blob repair is on the
        # volume, not in the transaction - so the connection is freed for the parse rather
        # than pinned across it.
        files, extraction = await read_recording(db, recording_id=existing.recording_id, release=True)
        try:
            await _rederive_recording(
                db,
                recording_id=existing.recording_id,
                dive_id=dive_id,
                ordinal=ordinal or 0,
                fresh=False,
                # No new bytes: these are the recording's own files, read again.
                joined=False,
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


async def load_recording_files(db: AsyncSession, *, recording_id: int) -> list[LoadedDiveFile]:
    """Every file of one recording, in attach order, with its bytes.

    **Attach order is `dive_file.id`** - a later upload takes a higher sequence value - and
    it is the whole of what "the first file that recorded it" means. Every caller of
    `extract_recording` gets its input from here so that ordering is stated once.

    Raises `BlobMissingError` if any of the bytes are gone, on `load_dive_file`'s terms: a
    partial read would silently apply the fill rule to a subset of the files and store the
    result as though it were the whole.
    """
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
            data=await blob_store.get(row.storage_key),
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
    # Read before the branch below can delete the recording, which is the only moment the
    # question is still answerable. `None` cannot happen - the file row named a live
    # recording a statement ago - and is read as the primary so that a row that somehow
    # vanished still gets the repair rather than silently skipping it.
    touched_primary = (ordinal or 0) == 0

    if remaining:
        await _rederive_recording(
            db,
            recording_id=row.recording_id,
            dive_id=row.dive_id,
            ordinal=ordinal or 0,
            fresh=True,
            # A deletion removes bytes; nothing arrived that could fill a cylinder.
            joined=False,
            files=remaining,
            extraction=await run_in_threadpool(extract_recording, remaining),
        )
    else:
        existing = await get_existing_profile(db, recording_id=row.recording_id)
        if existing is not None and not existing.is_reproducible:
            # Samples nothing here can produce again. The recording stays, file-less, which
            # is the same first-class shape a converted logbook import creates.
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
    await store_tech_scalars(db, dive_id=dive_id, scalars=dict.fromkeys(TECH_SCALAR_FIELDS), commit=False)
    if commit:
        await db.commit()


@dataclass(frozen=True, slots=True)
class TechBackfillReport:
    """What one run of `backfill_tech_fields` did.

    Dives and mixtures are counted separately because they are backfilled on different
    terms - the dive's scalars are filled where the files yield a reading the dive lacks, the
    mixture fields are applied only where the stored rows still demonstrably describe the
    parsed ones - so one number could not say whether a run went well. `mixtures_skipped` in
    particular is the interesting count: it is the diver having edited their cylinders since
    the import, which is a reason not to touch them rather than a failure.
    """

    examined: int = 0
    dives_updated: int = 0
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
    backstop for a mis-ordered list either: a parser that records no fractions at all
    leaves both `None` on every row, and `None` is explicitly not evidence of a mismatch
    (below) - on the parsed side or, since the columns became nullable, on the stored one.
    A 2026 Suunto Ocean export is exactly that shape - `_mixtures_from_cylinders`
    reconstructs its cylinders from sample data, which carries pressures and gas numbers
    but no `Gases` block - so on the one format whose `gas_number` is the file's own label
    rather than a synthesized position, an unordered read would swap the labels with
    nothing to catch it.

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
        # It is also silent data loss, because all three of these are client-writable
        # (`DiveMixtureBase` -> `DiveMixtureCreate`, and `PATCH /dive/{uuid}` replaces
        # mixtures wholesale). A FIT import produces `po2_limit=None` always and
        # `role=None` for any open-circuit gas; a diver who then sets 1.6 and `deco` on
        # their stage bottle has touched neither fraction, so the guard above still admits
        # the join and the backfill would have written both back to `NULL`.
        #
        # The dive's own scalars are overwritten outright a few lines up, and the asymmetry
        # is the point: nothing but the import writes those, so there is no edit to lose.
        # These three have another writer.
        #
        # A parser *correction* still lands, which is what the fill-only rule costs and
        # doesn't: where the file records a value the backfill overwrites as before, and it
        # declines only where the file has nothing to say.
        # Annotated rather than inferred: `dict` is invariant in its value type, so the
        # comprehension's own `dict[str, float | int | GasRole]` is not a `dict[str, object]`.
        values: dict[str, object] = {
            name: value
            for name, value in (
                ("po2_limit", parsed_mix.po2_limit),
                ("gas_number", parsed_mix.gas_number),
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
    """Re-read every primary recording's stored files for the columns nothing else fills.

    A second script rather than an extension of `backfill_profiles`, because the two select
    on different things and stop on different terms. That one is keyed to
    `PROFILE_EXTRACTOR_VERSION` and skips a recording whose profile is already current; these
    columns have no version of their own, and every candidate is re-read every run - which is
    cheap enough (a header parse, not a sample stream) and is what makes it correct to run
    again after a parser fix without a version to bump.

    Idempotent, and safe to run repeatedly.

    **It no longer clears.** It used to overwrite the dive's scalars outright, on the ground
    that "no other path writes them"; recordings add one. A logbook import that matches an
    existing recording and stores no bytes fills a blank `cns_end` from the document, and an
    outright rewrite here would delete that reading on the next run with nothing on the
    volume to recover it from. So a run now overwrites a scalar only with a value a stored
    file actually yields, and leaves untouched a value no stored file re-yields. A parser fix
    still reaches every dive whose file carries the reading, which is what this script is
    for; what it can no longer do is *null* a reading it has decided is bogus, and that is the
    accepted cost.

    **It walks primary recordings**, because the dive's scalars are the primary's - a second
    computer's CNS clock is its own device's arithmetic, and writing it onto the dive would
    attribute one machine's numbers to another. The device columns go the same way and are
    the reason this run matters on an upgraded instance: the migration leaves every migrated
    recording device-less, and one pass here fills them for every file-backed one.

    The mixture fields are best-effort - see `merge_mixture_fields` for why a dive whose
    cylinders have been edited is skipped rather than reconciled - and they are the primary
    recording's too.
    """
    # Imported here rather than at module scope, matching `backfill_profiles`: the crud
    # module is not otherwise part of this module's dependency surface, and keeping the
    # import next to its one use says so.
    from ..crud.crud_dive_mixtures import get_mixtures_for_dive
    from .cache_invalidation import invalidate_dive_caches

    stmt = (
        select(DiveRecording.id.label("recording_id"), DiveRecording.dive_id, DiveRecording.user_id)
        .where(DiveRecording.ordinal == 0)
        .order_by(DiveRecording.dive_id)
    )
    if parser_key is not None:
        # A recording is a candidate when any of its files was read by that parser, which is
        # what "I fixed the FIT parser, re-read the FITs" means for a recording holding two.
        stmt = stmt.where(
            select(DiveFile.id)
            .where(DiveFile.recording_id == DiveRecording.id, DiveFile.parser_key == parser_key)
            .exists()
        )
    if limit is not None:
        stmt = stmt.limit(limit)

    candidates = list(await db.execute(stmt))
    examined = dives_updated = mixtures_updated = mixtures_skipped = failed = 0
    # Dives written since the last commit, rather than a position in `candidates`. An
    # index-modulo test sits below several `continue`s, so a dive that failed on exactly
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
            # same terms as the parse failure below: a run that stopped here would report
            # less than one that finished and said how many dives are in this state.
            logger.error("Skipping dive %s: a stored file is missing from the volume", row.dive_id)
            failed += 1
            continue
        if not files:
            # A recording with nothing to re-read - a converted logbook import's. Not a
            # failure and not an update: there is no file, so there is nothing this script
            # can say about the dive that is not already stored.
            continue

        # Synchronously, unlike every request path: this runs from a one-shot script whose
        # event loop has nothing else on it, so the threadpool hop would buy nothing and cost
        # a handoff per recording in the corpus.
        extraction = extract_recording(files)
        if extraction.unreadable:
            # A file that parsed at import time and does not now is a parser regression, and
            # a run that reported only successes would hide it - the same reasoning as
            # `BackfillReport`'s five counts.
            logger.warning("Skipping dive %s: one of its stored exports could not be read", row.dive_id)
            failed += 1
            continue

        stored = await get_mixtures_for_dive(db, row.dive_id)
        updates = merge_mixture_fields(extraction.mixtures, stored)
        if updates is None:
            # `max`, not `len(stored)`: the count mismatch that refuses the merge includes
            # the diver having deleted every cylinder, and `len(stored)` is 0 there - so
            # the run reported "nothing skipped" for precisely the dive whose cylinders
            # were edited most. Whichever side has rows is what went unwritten.
            mixtures_skipped += max(len(stored), len(extraction.mixtures))

        if dry_run:
            dives_updated += 1
            mixtures_updated += len(updates or [])
            continue

        try:
            # A savepoint, so a dive the database rejects costs only that dive. Without it
            # the failure propagates out of this function and the enclosing `async with
            # local_session()` rolls back every uncommitted dive since the last batch
            # commit - and because nothing here advances a version column, the next run
            # reaches the same dive and dies the same way. The backfill could then never
            # get past it without hand-narrowing `--parser-key`.
            async with db.begin_nested():
                await fill_tech_scalars(db, dive_id=row.dive_id, scalars=extraction.scalars)
                await dive_recordings.fill_device_fields(
                    db,
                    recording_id=row.recording_id,
                    device=None if extraction.device_source is None else extraction.device_source.device,
                )
                for mixture_id, values in updates or []:
                    await db.execute(update(DiveMixture).where(DiveMixture.id == mixture_id).values(**values))
        except IntegrityError:
            # A parsed value the schema let through and the database won't take: a parser
            # unit bug, and the file that proves it is still attached to the dive. Counted
            # rather than raised, for the same reason as the parse failure above - a run
            # that stopped on it would report less than one that finished and said so.
            logger.warning("Skipping dive %s: its parsed values violate a constraint", row.dive_id, exc_info=True)
            failed += 1
            continue

        dives_updated += 1
        mixtures_updated += len(updates or [])
        touched_user_ids.add(row.user_id)

        pending += 1
        if pending >= _BACKFILL_BATCH_SIZE:
            await db.commit()
            pending = 0

    if not dry_run:
        await db.commit()
        # Cached dive reads embed both the scalars and the mixtures, so every dive this
        # run touched is now serving stale values. See the script for why this needs a
        # live Redis pool.
        for user_id in touched_user_ids:
            await invalidate_dive_caches(user_id)

    return TechBackfillReport(
        examined=examined,
        dives_updated=dives_updated,
        mixtures_updated=mixtures_updated,
        mixtures_skipped=mixtures_skipped,
        failed=failed,
    )
