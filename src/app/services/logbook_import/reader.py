"""Getting an import's files as far as parsed DiveJSON documents, and no further.

An import is a batch of files, each classified by its bytes and never by its name: the
archive `GET /export/archive` produces, whose `logbook.divejson` member is the app's own
document with the stored binaries beside it; a bare DiveJSON document; a file in any format
the `divejson` converter reads; a zip of any of those; or something nothing here reads.
Everything downstream works on one `ImportDocument` per file plus a way to fetch the bytes
a file brings, so the containers and the conversion are this module's problem alone.

**A zip that is not a full-export archive is opened, one level deep**, and its files join
the batch beside the request's own, so a zipped account export and a dropped folder are the
same batch. The `PK\\x03\\x04` sniff and `_open_archive`'s declared-size guard are what keep
a zip bomb off the disk; a zip inside a zip, and a second full-export archive, are refused
rows saying to import them on their own.

**Each file converts alone, as an archive of one member named by the file's SHA-256.** The
package scopes the identity of a record its source gave no id by the archive member it came
from, so a dive-computer file's dive - which never carries one - takes its identity from the
bytes: the same whenever the same bytes come again, under any name or in any folder, and
different for different bytes under one name. A record that carries its own id keeps the
identity that id gives it. `DECISIONS.md`, *"An import is its files imported one at a time"*,
has the rest.

**A file a reader claims and cannot convert is a row, not a refusal of the import.** The
package refuses a mixed or partly unreadable archive because a call with no per-file answer
cannot say which file failed; a batch has a row per file, and the most common stray - a run
the same watch recorded - is one a reader claims before refusing it. An import in which no
file reads answers as its first refused file would on its own.

**Converted in a thread**, as `POST /dive/parse` reads, with the same decoders and for the
same reason: *"Uploaded files are parsed in a thread, not on the event loop"* in
`DECISIONS.md`.

`MAX_DOCUMENT_SIZE` is the memory ceiling on every path, and it bounds the batch rather than
one file: every document and every file a reader claims - a zip's by its declared sizes, and
the archive's own document - are summed against it before anything converts, because every
converted document is held until the import is planned. A logbook large enough to matter
wants a streaming parser or an arq job. `DECISIONS.md`, *"Logbook import spools its upload
and still parses the document whole"*, carries the trade.
"""

import codecs
import hashlib
import json
import logging
import re
import shutil
import tempfile
import zipfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import IntEnum
from functools import partial
from typing import IO, TYPE_CHECKING, Any, BinaryIO, cast

import divejson
from divejson import Conversion, ConverterError, NonConformingOutputError, SourceTooLargeError, UnsupportedSourceError
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from ...core.utils.uploads import safe_filename
from ...schemas.dive_profile import MILLISECONDS_PER_SECOND
from ...schemas.export import (
    DIVEJSON_FORMAT,
    DIVEJSON_PRODUCER_KEY,
    DIVEJSON_VERSION,
    PROFILE_AXIS_MARKER,
    PROFILE_AXIS_MILLISECONDS,
)
from ...schemas.logbook_import import (
    ConversionConverter,
    ConversionNoteGroup,
    ConversionReport,
    ImportDocument,
    ImportMemberNotKept,
    ImportNoteCode,
)
from ..dive_files import MAX_DIVE_FILE_SIZE, FileExtraction, extraction_of
from ..dive_reader import ReadDive, formats_this_build_reads, is_logbook_format
from ..export.archive import DIVEJSON_NAME

if TYPE_CHECKING:
    from .parts import ImportPart

logger = logging.getLogger(__name__)

# What `zipfile` raises when a member cannot be inflated, as opposed to when the container
# cannot be opened. All three are ordinary states of a file somebody actually has:
# `RuntimeError` for a password-protected archive (the commonest by far - a diver zips
# their export with a password and hands it over), `BadZipFile` for a CRC mismatch from a
# truncated or bit-rotted member, and `NotImplementedError` for a compression method this
# build has no decoder for. `OSError` covers a spool that cannot be read back. None is a
# subclass of anything the route translates, so uncaught they are a 500 - which is the
# wrong answer to every one of them.
_MEMBER_READ_FAILURES = (RuntimeError, zipfile.BadZipFile, NotImplementedError, OSError)

# A document, and what one import plans in all. Generous against the reference
# implementation's own output - the demo logbook is kilobytes and a thousand sampled dives
# is tens of megabytes - and the number that actually bounds this endpoint's memory, since
# every document is parsed whole and held until the import is planned.
MAX_DOCUMENT_SIZE = 100 * 1024 * 1024  # 100 MB

# A zip part, and a whole request. Five times the document cap because an archive's
# binaries are what dominate it.
MAX_ARCHIVE_SIZE = 500 * 1024 * 1024  # 500 MB

# What a zip's members may sum to *uncompressed*, checked against the central directory
# before a single byte is inflated. Zip compresses text a thousand to one, so the transfer
# cap above bounds nothing on its own: without this a 5 MB upload could ask for gigabytes of
# temp file. Twice the archive cap leaves room for an honest logbook whose documents deflate
# well while refusing anything shaped like a bomb.
MAX_ARCHIVE_EXTRACTED_SIZE = 2 * MAX_ARCHIVE_SIZE

# The most files one zip may hold. This bounds the *walk* rather than the bytes: to decide a
# member's format its head has to be inflated - a FIT's magic sits eight bytes into the
# member, so no listing can answer it - and a zip of half a million tiny files is a lot of
# work inside one request even when it fits under the planning bound. Deliberately not a
# promise about how many dives go in one import: `MAX_DOCUMENT_SIZE` over the batch is what a
# real dive-computer export meets first, and at 30 KB a FIT file that is a few thousand of
# them. The count itself is refused off the directory listing, before anything is opened.
MAX_ARCHIVE_MEMBERS = 5000

# Zip's local file header. Sniffed rather than trusting the filename or the client's
# content type, on the same principle as `certification_files.sniff_content_type`: the
# bytes say what they are.
_ZIP_MAGIC = b"PK\x03\x04"

_READ_CHUNK_SIZE = 1024 * 1024

# The major version this reader implements. A reader accepts any document whose *major*
# version it implements and ignores what it does not recognize (spec §§4, 5.6, 7), so a
# `1.7` document is read as far as 1.0 defines and a `2.0` one is refused outright.
_SUPPORTED_MAJOR = DIVEJSON_VERSION.split(".", 1)[0]


class UnsupportedImportError(Exception):
    """No reader claims these bytes - a 415.

    Deliberately the same distinction `POST /dive/parse` draws: 415 is "this is not a file
    I can read", 422 is "this is one, and it is broken". The two land in different places
    in a client, and collapsing them would tell a diver their export is corrupt when they
    have handed over a photo. Since the converter joined this path, "a file I can read" is
    a DiveJSON document, the export archive, or any format `divejson.read_formats()` names,
    so the message says which rather than naming one format.
    """


class MalformedImportError(Exception):
    """A reader claimed the file and could not read it - a 422."""


class ImportTooLargeError(Exception):
    """A file or the whole import is past its cap - a 413, matching
    `read_upload_within_limit`'s answer everywhere else in the app."""


class DuplicateMemberError(MalformedImportError):
    """A JSON object in the document carries the same member name twice (spec §9).

    A `MalformedImportError` because it is exactly that: `json` would otherwise keep the
    last value silently, and a document whose reader and writer disagree about which of two
    `max_depth` members is real is not something to guess at.
    """


# A refusal of one file, which is also what the import answers when no file of it reads.
type Refusal = UnsupportedImportError | MalformedImportError | ImportTooLargeError


def _reject_duplicate_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise DuplicateMemberError(f"duplicate member name {key!r}")
        obj[key] = value
    return obj


def parse_document(text: str | bytes) -> Any:
    """Parse document text as JSON, rejecting duplicate member names (spec §9).

    The app's own rather than `divejson.parse_document`, which is the same rule and takes
    `str` where an upload arrives as bytes. The duplicate-member refusal is the one §9 rule
    the importer has to enforce itself: `json` keeps the last value silently, so a document
    with two `max_depth` members has no reading a parser can pick honestly.
    """
    return json.loads(text, object_pairs_hook=_reject_duplicate_members)


@dataclass(frozen=True, slots=True)
class KeptFile:
    """A file that is one recording's file, and what keeping it on that recording needs.

    The rule the dive form applies to a file it is handed: a file converting to one dive
    carrying at most one computer's record of it, whatever its format. `key` is the name the
    loaded import serves the bytes under, beside an archive's members; `extraction` is what
    the file yields read through the one reader, taken off the conversion the import already
    ran rather than off a second one.
    """

    key: str
    sha256: str
    filename: str
    format: str
    size: int
    extraction: FileExtraction


@dataclass(slots=True)
class LoadedImport:
    """One file of an import as a parsed document, plus the bytes it brings.

    Owns nothing: the spools and containers it reads from belong to the `LoadedBatch` it is
    part of, which closes them.

    `conversion` and `source_format` are the converter's, and both are `None` for a native
    DiveJSON document or archive. Nothing below this module reads either: the converted
    document enters the planner through `ImportDocument` like any other. They are here
    because the *report* is a route concern and the route has nothing else to build it from.

    `is_archive` says this file is the full-export archive, whose container carries the
    stored binaries its document names. `kept` is the file itself where it is one
    recording's file, and `not_kept` says why a file read into dives is not.
    """

    document: ImportDocument
    digest: str
    is_archive: bool
    conversion: Conversion | None
    source_format: str | None
    _archive: zipfile.ZipFile | None = None
    # What `read_as_written` read the way a pre-change writer meant it, for the report.
    read_as_written: list[ReaderNote] = field(default_factory=list)
    kept: KeptFile | None = None
    not_kept: ImportMemberNotKept | None = None
    _read_kept: Callable[[], bytes] | None = None

    def member_size(self, path: str) -> int | None:
        """The size of one file this import can store, or `None` when it is not there.

        An archive's member by its declared uncompressed size, or the kept file under its
        key. `path` is a document-supplied string and is used as a **zip member name and
        nothing else** - no filesystem call takes it, so a `../../.bashrc` in it addresses a
        member that does not exist rather than a path outside anything.

        Split from `read_member` so the *preview* can say which files would restore without
        inflating a byte of any of them, and so "not in this archive" and "too big to store"
        stay two different answers to the diver. It comes out of the central directory, so
        it is a dictionary lookup.
        """
        if self.kept is not None and path == self.kept.key:
            return self.kept.size
        if self._archive is None:
            return None
        try:
            return self._archive.getinfo(path).file_size
        except KeyError:
            return None

    def read_member(self, path: str) -> bytes | None:
        """One file's bytes - an archive's member or the kept file - or `None` when they
        cannot be had.

        Unbounded on purpose: every caller has already been through `member_size` and
        refused anything over its own cap, and a container was refused whole if its declared
        sizes summed past `MAX_ARCHIVE_EXTRACTED_SIZE`. `zipfile` verifies the CRC on the way
        out, which is what makes those declared sizes worth trusting; the digest the caller
        then checks is the end-to-end guarantee.

        **A file that will not inflate is `None` rather than an exception**, because the
        caller is the writer and it already has the right answer for a file it cannot have:
        skip it, report it, and leave the dive. Letting the failure out would abort a
        half-written import over one corrupt c-card scan, which is the opposite trade from
        every other file decision here.
        """
        if self.kept is not None and path == self.kept.key and self._read_kept is not None:
            try:
                return self._read_kept()
            except _MEMBER_READ_FAILURES:
                logger.warning("A file could not be read back during an import", exc_info=True)
                return None
        if self._archive is None:
            return None
        try:
            info = self._archive.getinfo(path)
        except KeyError:
            return None
        try:
            return self._archive.read(info)
        except _MEMBER_READ_FAILURES:
            logger.warning("An archive member could not be read during an import", exc_info=True)
            return None


def _validate_envelope(raw: Any) -> ImportDocument:
    """The parsed JSON as an `ImportDocument`, or the right refusal.

    The `format` and `version` checks come first and answer 415, because they are what say
    whether this is a DiveJSON document at all - a reader dispatches on them before parsing
    further (spec §4). Everything after that is a 422: it *is* a DiveJSON document, and it
    is broken.
    """
    if not isinstance(raw, dict):
        raise UnsupportedImportError(
            f"This file is not a DiveJSON document. This app also reads: {formats_this_build_reads()}."
        )
    if raw.get("format") != DIVEJSON_FORMAT:
        raise UnsupportedImportError(
            "This file is not a DiveJSON document - its `format` member says otherwise. "
            f"This app also reads: {formats_this_build_reads()}."
        )

    version = raw.get("version")
    if not isinstance(version, str) or version.split(".", 1)[0] != _SUPPORTED_MAJOR:
        raise UnsupportedImportError(
            f"This document declares DiveJSON version {version!r}, and this app implements {DIVEJSON_VERSION}. "
            "Minor versions are additive and are read; a different major version is not."
        )

    try:
        return ImportDocument.model_validate(raw)
    except ValidationError as exc:
        raise MalformedImportError(_first_error(exc)) from exc


@dataclass(frozen=True, slots=True)
class ReaderNote:
    """One line for the import report, decided before the planner runs."""

    code: ImportNoteCode
    message: str
    collection: str | None = None
    uuid: str | None = None


# `divejson convert` names itself with this constant; its releases before this one wrote the
# profile axis in seconds and the readouts on the dive, under the same member names.
_CONVERTER_GENERATOR = "divejson convert"
_CONVERTER_MILLISECONDS_SINCE = (0, 13, 0)
_RELEASE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
_READOUTS = ("surface_pressure", "cns_start", "cns_end", "otu_start", "otu_end")


def _members(value: Any, name: str) -> dict[str, Any]:
    member = value.get(name) if isinstance(value, dict) else None
    return member if isinstance(member, dict) else {}


def _list(value: Any, name: str) -> list[Any]:
    member = value.get(name) if isinstance(value, dict) else None
    return member if isinstance(member, list) else []


def _written_before_the_axis_moved(raw: dict[str, Any]) -> bool:
    """Whether a writer this app knows produced `raw` before the profile axis moved.

    Two such writers. **This app**, whose every export writes its producer key on the diver
    and, from the change on, the axis marker at the root: a document with the first and not
    the second is one of its own from before. Its `generator.name` is `settings.APP_NAME`,
    which an operator configures, and its `generator.version` is shared by an edge build and
    the release before it, so neither can be the key. **The published converter**, whose
    generator name is a constant and whose releases below `_CONVERTER_MILLISECONDS_SINCE`
    wrote seconds; the version compares as a tuple, `0.9.0` being below `0.13.0`.
    """
    producer = _members(raw, "extensions").get(DIVEJSON_PRODUCER_KEY)
    marked = isinstance(producer, dict) and producer.get(PROFILE_AXIS_MARKER) == PROFILE_AXIS_MILLISECONDS
    if DIVEJSON_PRODUCER_KEY in _members(_members(raw, "diver"), "extensions") and not marked:
        return True

    generator = _members(raw, "generator")
    version = generator.get("version")
    release = _RELEASE.match(version) if isinstance(version, str) else None
    if generator.get("name") != _CONVERTER_GENERATOR or release is None:
        return False
    return tuple(int(part or 0) for part in release.groups()) < _CONVERTER_MILLISECONDS_SINCE


def _in_milliseconds(profile: dict[str, Any]) -> None:
    """A pre-change profile's axis, multiplied into milliseconds in place."""
    series = [value for value in profile.values() if isinstance(value, dict)] + _list(profile, "pressures")
    for entry in series:
        times = entry.get("times") if isinstance(entry, dict) else None
        if isinstance(times, list):
            entry["times"] = [t * MILLISECONDS_PER_SECOND if isinstance(t, int) else t for t in times]
    for event in _list(profile, "events"):
        if isinstance(event, dict) and isinstance(event.get("time"), int):
            event["time"] *= MILLISECONDS_PER_SECOND
    if isinstance(profile.get("duration"), int):
        profile["duration"] *= MILLISECONDS_PER_SECOND


def _sightings_from_species_uuids(raw: dict[str, Any]) -> list[ReaderNote]:
    """A dive's retired `species_uuids`, read as sightings with no count and no note.

    Keyed on the member rather than on who wrote it: it is retired, so a dive carrying it is
    from before the change whatever produced it. A dive that also carries `sightings` keeps
    those and the old list is ignored, as any undefined member is (§5.6).
    """
    dives = 0
    for dive in _list(raw, "dives"):
        if not isinstance(dive, dict) or "species_uuids" not in dive or "sightings" in dive:
            continue
        uuids = _list(dive, "species_uuids")
        del dive["species_uuids"]
        dive["sightings"] = [{"species_uuid": value} for value in uuids]
        dives += bool(uuids)
    if not dives:
        return []
    return [
        ReaderNote(
            ImportNoteCode.READ_AS_WRITTEN,
            "This logbook was written before DiveJSON gave a sighting a count and a note, so the species of "
            f"{dives} dive(s) were read as sightings with neither.",
            collection="dives",
        )
    ]


def read_as_written(raw: dict[str, Any]) -> list[ReaderNote]:
    """Read a document written before the format moved, as its writer meant it.

    Rewrites `raw` in place into the current shape and says what it read, one report line
    per kind: a dive's `species_uuids` as sightings, whoever wrote it; and for a writer this
    app knows, the profile axis in seconds, multiplied; the readouts on the dive, onto its
    first recording - minting one where the dive has none, a recording of readouts alone;
    `water_type: "en13319"` onto that recording's `salinity`, dropped where the dive has
    neither a recording nor a readout to carry it; and a cylinder's `po2_limit` as
    `ppo2_limit`. Anything else passes untouched, and past the sightings a document no known
    writer produced is not looked at: without this every old spelling would vanish silently,
    the importer ignoring what it does not know (§5.6).
    """
    sightings = _sightings_from_species_uuids(raw)
    if not _written_before_the_axis_moved(raw):
        return sightings

    axes = readouts = salinities = limits = 0
    notes: list[ReaderNote] = []
    for dive in _list(raw, "dives"):
        if not isinstance(dive, dict):
            continue
        recordings = [recording for recording in _list(dive, "recordings") if isinstance(recording, dict)]
        profiles = [profile for recording in recordings if (profile := _members(recording, "profile"))]
        for profile in profiles:
            _in_milliseconds(profile)
        axes += bool(profiles)

        carried = {member: dive.pop(member) for member in _READOUTS if member in dive}
        if carried:
            if not recordings:
                recordings = [{}]
                dive["recordings"] = recordings
            for member, value in carried.items():
                recordings[0].setdefault(member, value)
            readouts += 1

        if dive.get("water_type") == "en13319":
            del dive["water_type"]
            if recordings:
                recordings[0].setdefault("salinity", "en13319")
                salinities += 1
            else:
                notes.append(
                    ReaderNote(
                        ImportNoteCode.VALUE_DROPPED,
                        "This dive's water type was EN 13319, a dive computer's setting rather than a kind of water, "
                        "and there is no recording of it to carry the setting, so it was dropped",
                        collection="dives",
                        uuid=str(dive.get("uuid")),
                    )
                )

        for cylinder in _list(dive, "cylinders"):
            if isinstance(cylinder, dict) and "po2_limit" in cylinder:
                cylinder.setdefault("ppo2_limit", cylinder.pop("po2_limit"))
                limits += 1

    summary = [
        ReaderNote(ImportNoteCode.READ_AS_WRITTEN, message, collection="dives")
        for count, message in (
            (
                axes,
                "This logbook was written before DiveJSON's profile times became milliseconds, so the profiles of "
                f"{axes} dive(s) were read in seconds, as written.",
            ),
            (
                readouts,
                "This logbook was written before DiveJSON moved a dive computer's CNS, OTU and surface pressure onto "
                f"its recording, so those of {readouts} dive(s) were read onto the dive's first recording.",
            ),
            (
                salinities,
                "This logbook was written before DiveJSON moved the EN 13319 setting onto the recording, so "
                f"{salinities} dive(s) carry it as their first recording's salinity rather than as a water type.",
            ),
            (
                limits,
                "This logbook was written before DiveJSON renamed a cylinder's `po2_limit` to `ppo2_limit`, so "
                f"{limits} cylinder(s) kept their ppO2 limit.",
            ),
        )
        if count
    ]
    return sightings + summary + notes


# What a document written before a dive site's `location` became an object looks like from
# here, and what to tell the diver holding one. §6.9 defines one shape and this reader
# implements it, so the refusal is the schema working rather than a gap - but a member path
# and "input should be a valid dictionary" sends nobody anywhere, and this is a cause a
# diver can act on with one click.
PRE_CHANGE_SITE_LOCATION = (
    "This logbook was exported before a dive site's location became a structured place, so this app cannot read "
    "it: its sites carry a location as plain text where the format now defines an object with a name of its own. "
    "Export your logbook again from the app and import that file."
)


def _is_pre_change_site_location(exc: ValidationError) -> bool:
    """Whether the document spells a site's `location` as the string 1.0 used to define.

    Keyed on the member path and on the value's own type rather than on Pydantic's error
    code, which names an implementation detail of how the union is built and would stop
    matching on an upgrade.
    """
    return any(
        len(error["loc"]) >= 3
        and error["loc"][0] == "sites"
        and error["loc"][-1] == "location"
        and isinstance(error.get("input"), str)
        for error in exc.errors()
    )


def _first_error(exc: ValidationError) -> str:
    """One sentence naming where the document broke.

    The first error rather than all of them: a structurally wrong document produces one
    per record, and a diver needs the location more than the count.
    """
    if _is_pre_change_site_location(exc):
        return PRE_CHANGE_SITE_LOCATION
    errors = exc.errors()
    if not errors:
        return "This DiveJSON document could not be read."
    first = errors[0]
    location = ".".join(str(part) for part in first["loc"]) or "the document"
    return f"This DiveJSON document could not be read: {location}: {first['msg']}."


def _unrecognized() -> UnsupportedImportError:
    return UnsupportedImportError(
        "This file is not a logbook this app can read. It reads a DiveJSON document, the full-export archive, "
        f"and dive-computer exports in these formats: {formats_this_build_reads()}."
    )


def _claims_to_be_json(head: bytes) -> bool:
    """Whether a bounded head is text opening an object, i.e. a file claiming to be JSON.

    The one thing standing between a truncated `.divejson` - the app's own export, cut off
    by a failed download, and much the commonest real failure this endpoint meets - and
    being told the app does not recognise its own format. A file that opens `{` claimed to
    be a document; anything else that neither sniffs nor parses never did.
    """
    if head.startswith(codecs.BOM_UTF8):
        head = head[len(codecs.BOM_UTF8) :]
    return head.lstrip()[:1] == b"{"


def _conversion_moment() -> datetime:
    """The `exported_at` a conversion stamps on its output.

    Its own function so that a test can say "convert these bytes as if it were another
    hour" and hold the rest of the document to being identical - which is the whole claim
    behind re-converting on apply instead of spooling the preview's result.
    """
    return datetime.now(UTC)


def _converted_from(document: Any, fallback: str | None) -> str | None:
    """Which format the converted document says it came from.

    `converting.md`'s *Provenance* block is where the library records which reader read a
    file, and a document that carries none leaves the fallback - what this module sniffed.
    """
    if not isinstance(document, dict):
        return fallback
    extensions = document.get("extensions")
    block = extensions.get(divejson.PRODUCER_KEY) if isinstance(extensions, dict) else None
    value = block.get("converted_from") if isinstance(block, dict) else None
    return value if isinstance(value, str) else fallback


_CONVERTER_BUG = (
    "This file was recognised, and converting it produced a logbook this app cannot read. That is a bug in the "
    "converter rather than anything wrong with your file - please report it."
)


def _one_member_archive(source: IO[bytes], name: str) -> IO[bytes]:
    """`source`'s bytes as the only member of a zip, the member called `name`.

    Stored rather than deflated - the converter reads it back at once - and on disk rather
    than in memory, the converter being about to hold the member whole itself.
    """
    wrapper = tempfile.TemporaryFile()
    try:
        with zipfile.ZipFile(wrapper, "w", zipfile.ZIP_STORED) as archive, archive.open(name, "w") as member:
            shutil.copyfileobj(source, member, _READ_CHUNK_SIZE)
    except BaseException:
        wrapper.close()
        raise
    wrapper.seek(0)
    return wrapper


def _convert(wrapper: IO[bytes]) -> Conversion:
    """Convert a one-member archive, with this module's caps on it.

    `exported_at` is passed rather than defaulted because the library's default is *now, in
    the local zone*, and this is the one value in a converted document that is not a function
    of the source. Preview and apply convert the same bytes minutes apart and must plan
    identically, so nothing downstream reads it - `divejson.compared` drops it, and the
    planner never looks.
    """
    # `convert` is annotated `BinaryIO`, which differs from the `IO[bytes]` spooled here only
    # in what `__enter__` returns - and `convert` never enters it.
    return divejson.convert(
        cast(BinaryIO, wrapper),
        exported_at=_conversion_moment(),
        max_members=1,
        max_member_size=MAX_DOCUMENT_SIZE,
    )


def _renamed(conversion: Conversion, digest: str, name: str) -> Conversion:
    """The conversion with each finding's path under the file's name rather than its digest.

    The package prefixes a member's paths with the member's name, which for an import's file
    is its digest; a diver reading the report knows the file by what they called it.
    """
    prefix = f"{digest}/"
    notes = tuple(
        divejson.Note(f"{name}/{note.where[len(prefix) :]}", note.message, note.kind)
        if note.where.startswith(prefix)
        else note
        for note in conversion.notes
    )
    return Conversion(conversion.document, notes)


def _convert_file(source: Source, *, digest: str, name: str, claimed: str, size: int) -> LoadedImport:
    """One file a reader claims, converted alone and read as one recording's file where it is one.

    Pure CPU and file reads, so the caller runs it in a thread. Every refusal below is the
    converter's, translated into this module's three so the route keeps one taxonomy; the
    last arms are the ones that matter, a `ConverterError` this build has never seen and
    anything the package's decoders let out landing as a 422 rather than a 500 - the
    registry may grow one at any pin bump, and the decoders read bytes a stranger supplied.
    """
    with source() as stream, _one_member_archive(stream, digest) as wrapper:
        try:
            conversion = _convert(wrapper)
        except SourceTooLargeError as exc:
            # The converter's own sentence: it names which of its bounds fired - the FIT
            # reader refuses past a hundred thousand messages - and restating them here would
            # let this message claim a cause that cannot be one.
            raise ImportTooLargeError(
                f"This file is past what one import reads - {_named(exc, digest, name)}."
            ) from exc
        except UnsupportedSourceError as exc:
            raise UnsupportedImportError(
                f"This file is not one this app can read - {_named(exc, digest, name)}."
            ) from exc
        except NonConformingOutputError as exc:
            # A converter bug, not a diver's file: the library validates its own output and
            # this is it saying no. Logged with a traceback because nobody else will see it.
            logger.exception("The converter produced a non-conforming document during a logbook import")
            raise MalformedImportError(_CONVERTER_BUG) from exc
        except ConverterError as exc:
            raise MalformedImportError(f"This file could not be converted: {_named(exc, digest, name)}.") from exc
        except Exception as exc:
            logger.exception("Unexpected error converting a %s file during a logbook import", claimed)
            raise MalformedImportError(f"This file could not be converted: {str(exc) or type(exc).__name__}.") from exc

    try:
        document = _validate_envelope(conversion.document)
    except (UnsupportedImportError, MalformedImportError) as exc:
        logger.exception("A converted document did not survive this app's own envelope check")
        raise MalformedImportError(_CONVERTER_BUG) from exc
    if _records_nothing(document):
        # A run the same watch recorded, say: the reader reads the file and finds no dive in
        # it, and a file that brings nothing is one the diver should hear about by name. A
        # logbook with no dive that still names its diver or holds other records - this app's
        # own UDDF export of an account with no dives - imports what it holds.
        raise MalformedImportError("This file records no dive, so there is nothing in it to import.")

    fmt = _converted_from(conversion.document, claimed) or claimed
    loaded = LoadedImport(
        document=document,
        digest=digest,
        is_archive=False,
        conversion=_renamed(conversion, digest, name),
        source_format=fmt,
    )
    if not document.dives:
        # A logbook of other records alone: no dive to keep the file on.
        return loaded
    if len(document.dives) > 1:
        loaded.not_kept = ImportMemberNotKept.SEVERAL_DIVES
    elif len(document.dives[0].recordings) > 1:
        loaded.not_kept = ImportMemberNotKept.SEVERAL_RECORDINGS
    elif size > MAX_DIVE_FILE_SIZE:
        loaded.not_kept = ImportMemberNotKept.TOO_LARGE
    else:
        dive = document.dives[0]
        raw = conversion.document["dives"][0].get("started_at")
        read = ReadDive(
            format=claimed,
            dive=dive,
            recording=dive.recordings[0] if dive.recordings else None,
            started_at=raw if isinstance(raw, str) else None,
        )
        loaded.kept = KeptFile(
            key=digest,
            sha256=digest,
            filename=safe_filename(name, default="dive-file"),
            format=claimed,
            size=size,
            extraction=extraction_of(read),
        )
        loaded._read_kept = lambda: _read_all(source)
    return loaded


def _records_nothing(document: ImportDocument) -> bool:
    """No dive, no other record and no diver: what a converted activity that is not a dive is."""
    return document.diver is None and not any(value for _, value in document if isinstance(value, list))


def _named(exc: Exception, digest: str, name: str) -> str:
    """The converter's sentence, with the file's name where it names the digest."""
    return str(exc).replace(digest, name)


def _read_all(source: Source) -> bytes:
    with source() as stream:
        return stream.read()


def _load_document(read: Callable[[], bytes], *, digest: str) -> LoadedImport:
    """A bare DiveJSON document: parsed, read as its writer meant it, and validated."""
    raw_bytes = read()
    try:
        raw = parse_document(raw_bytes)
    except DuplicateMemberError:
        raise
    except UnicodeDecodeError as exc:
        raise MalformedImportError("This logbook document is not valid JSON: it is not UTF-8 text.") from exc
    except json.JSONDecodeError as exc:
        raise MalformedImportError(f"This logbook document is not valid JSON: {exc.msg} at line {exc.lineno}.") from exc
    notes = read_as_written(raw) if isinstance(raw, dict) else []
    return LoadedImport(
        document=_validate_envelope(raw),
        digest=digest,
        is_archive=False,
        conversion=None,
        source_format=None,
        read_as_written=notes,
    )


def _load_archive(archive: zipfile.ZipFile, *, digest: str) -> LoadedImport:
    """The full-export archive: its document, and the container its files are read from."""
    info = archive.getinfo(DIVEJSON_NAME)
    if info.file_size > MAX_DOCUMENT_SIZE:
        raise ImportTooLargeError(
            f"The {DIVEJSON_NAME} inside this archive is larger than the {MAX_DOCUMENT_SIZE // (1024 * 1024)} MB limit."
        )
    try:
        raw_bytes = archive.read(info)
    except _MEMBER_READ_FAILURES as exc:
        # A 422 rather than a 415: this *is* an archive of the shape this app produces, and
        # the logbook inside it cannot be read. The message names the two causes a diver can
        # do something about, because "could not be read" on its own sends nobody anywhere.
        raise MalformedImportError(
            f"The {DIVEJSON_NAME} inside this archive could not be read. If the archive is password-protected, "
            "extract it first and import the document on its own; otherwise the file is damaged."
        ) from exc
    try:
        raw = parse_document(raw_bytes)
    except DuplicateMemberError:
        raise
    except UnicodeDecodeError as exc:
        raise MalformedImportError("This logbook document is not valid JSON: it is not UTF-8 text.") from exc
    except json.JSONDecodeError as exc:
        raise MalformedImportError(f"This logbook document is not valid JSON: {exc.msg} at line {exc.lineno}.") from exc
    notes = read_as_written(raw) if isinstance(raw, dict) else []
    return LoadedImport(
        document=_validate_envelope(raw),
        digest=digest,
        is_archive=True,
        conversion=None,
        source_format=None,
        _archive=archive,
        read_as_written=notes,
    )


# How many distinct `(kind, message)` pairs a report carries. The converter's own note list
# is unbounded and a thousand-dive logbook with one habit per record makes thousands of
# them, so a cap has to live somewhere; grouping is the only place it can, since the counts
# stay complete either way. Far below `planner.MAX_NOTES` on purpose - a group is a *kind
# of* finding, and a hundred distinct ones is already more than any adapter emits.
MAX_CONVERSION_GROUPS = 100

# Enough to point at without becoming the report. A diver who wants every path has the
# source file; three says "here, and here, and elsewhere".
MAX_CONVERSION_WHERES = 3

# The *package*, whose version is the pin a report is attributable to - not the document's
# `generator.name`, which is the CLI's name and already on `ImportPreview.generator`.
_CONVERTER_NAME = "divejson"

# Only reachable if a converted document arrived without the provenance member that says
# which format it came from, which the converter writes on every path it has. Named rather
# than guessed at, so a report that somehow lands here says so instead of claiming a format.
_UNKNOWN_SOURCE_FORMAT = "unknown"


# What `ConversionReport.format` says of an import whose converted files were not all one
# format.
MIXED_FORMATS = "mixed"


def conversion_report(loaded: Sequence[LoadedImport]) -> ConversionReport | None:
    """What converting an import's files could not carry, or `None` when none was converted.

    One report over every converted file: its format is theirs where they share one and
    `mixed` where they do not, and its groups are the union of theirs, grouped by the
    library's own `grouped` - the one the CLI prints - over their findings in the import's
    order. Built here rather than in the browser so preview and result render one shape.
    """
    converted = [one for one in loaded if one.conversion is not None]
    if not converted:
        return None
    formats = {one.source_format or _UNKNOWN_SOURCE_FORMAT for one in converted}
    notes = tuple(note for one in converted if one.conversion is not None for note in one.conversion.notes)
    groups = Conversion({}, notes).grouped()
    return ConversionReport(
        format=formats.pop() if len(formats) == 1 else MIXED_FORMATS,
        converter=ConversionConverter(name=_CONVERTER_NAME, version=divejson.__version__),
        groups=[
            ConversionNoteGroup(
                kind=group.kind,
                message=group.message,
                count=len(group.wheres),
                wheres=group.wheres[:MAX_CONVERSION_WHERES],
            )
            for group in groups[:MAX_CONVERSION_GROUPS]
        ],
        groups_truncated=max(len(groups) - MAX_CONVERSION_GROUPS, 0),
    )


# ------------------------------------------------------------------ the batch

# Where a way of reading a file's bytes from the start comes from: a part's spool, rewound,
# or a zip's member, opened afresh.
Source = Callable[[], AbstractContextManager[IO[bytes]]]


class FileKind(IntEnum):
    """What a file of the batch is, by its bytes - and, by value, the order the import reads
    the kinds in.

    The full-export archive first, then DiveJSON documents, then logbooks, then one
    computer's files, so that where a logbook and a computer's file describe one dive the
    computer's recording joins the logbook's dive rather than the other way round - a record
    attached to a dive brings none of its own dive's values, and a logbook's number, notes
    and sites are the diver's. Zips and what nothing reads sort after them, for the rows.
    """

    ARCHIVE = 0
    DOCUMENT = 1
    LOGBOOK = 2
    COMPUTER = 3
    ZIP = 4
    UNREAD = 5


@dataclass(slots=True, kw_only=True)
class BatchRow:
    """One file of the batch: a part of the request, or a file a zip among them held.

    `loaded` is set for a file that reads and `refusal` for one that does not. `container`
    is the row index of the zip a member came out of, and `opened` a zip's own count of the
    files it opened into.
    """

    part: int
    name: str
    size: int
    sha256: str
    kind: FileKind = FileKind.UNREAD
    format: str | None = None
    container: int | None = None
    opened: int | None = None
    loaded: LoadedImport | None = None
    refusal: Refusal | None = None
    _source: Source | None = None
    _archive: zipfile.ZipFile | None = None
    _info: zipfile.ZipInfo | None = None
    _zip: BatchRow | None = None


@dataclass(slots=True)
class LoadedBatch:
    """Every file of an import, in the one order the import reads them in.

    That order is the batch's: by kind (`FileKind`), then by name, ties broken by digest. A
    browser's folder walk is not sorted, so nothing may depend on the order files arrive in.
    Holds the containers it opened, so it is a context manager and the caller uses it as one;
    the request's spools are the request's to close.
    """

    rows: list[BatchRow] = field(default_factory=list)
    _containers: list[zipfile.ZipFile] = field(default_factory=list)

    def __enter__(self) -> LoadedBatch:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        for container in self._containers:
            container.close()

    @property
    def read(self) -> list[BatchRow]:
        """The rows that read into a document, in the batch's order."""
        return [row for row in self.rows if row.loaded is not None]

    @property
    def documents(self) -> list[LoadedImport]:
        return [row.loaded for row in self.rows if row.loaded is not None]


@contextmanager
def _rewound(spool: IO[bytes]) -> Iterator[IO[bytes]]:
    """A part's spool from its start, left open: the request owns it."""
    spool.seek(0)
    yield spool


def _hidden(name: str) -> bool:
    """Packaging rather than a file: a dot-file, or anything under a `__MACOSX/` shadow tree -
    the rule the package applies to an archive's members, so a folder dropped from a Mac and
    the same folder zipped carry the same files."""
    parts = name.replace("\\", "/").split("/")
    return "__MACOSX" in parts or parts[-1].startswith(".")


def _open_archive(buffer: IO[bytes]) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(buffer)
    except (zipfile.BadZipFile, OSError, EOFError) as exc:
        raise MalformedImportError("This looks like a zip archive but could not be opened.") from exc

    declared = sum(info.file_size for info in archive.infolist())
    if declared > MAX_ARCHIVE_EXTRACTED_SIZE:
        archive.close()
        raise ImportTooLargeError(
            f"This archive expands to more than {MAX_ARCHIVE_EXTRACTED_SIZE // (1024 * 1024)} MB and was not opened."
        )
    return archive


def _holds(archive: zipfile.ZipFile, name: str) -> bool:
    try:
        archive.getinfo(name)
    except KeyError:
        return False
    return True


def _classify(row: BatchRow, head: bytes, source: Source) -> None:
    """Decide a file's kind off a bounded head of it, the way `divejson.sniff` decides a format.

    A file that opens `{` and no reader claims is a DiveJSON document, so a truncated export
    of this app's own - much the commonest real failure - is told its document is broken
    rather than that the app does not recognise its own format.
    """
    claimed = divejson.sniff(head)
    if claimed is not None and claimed in divejson.read_formats():
        row.kind = FileKind.LOGBOOK if is_logbook_format(claimed) else FileKind.COMPUTER
        row.format = claimed
        row._source = source
    elif _claims_to_be_json(head):
        row.kind = FileKind.DOCUMENT
        row.format = "divejson"
        row._source = source
    else:
        row.refusal = _unrecognized()


def _open_zip(archive: zipfile.ZipFile, zip_row: BatchRow) -> list[BatchRow]:
    """A zip's files as rows of their own, one level deep and behind the zip's own guards."""
    infos = [
        info for info in archive.infolist() if not info.is_dir() and not _hidden(info.filename) and info.file_size > 0
    ]
    if not infos:
        zip_row.refusal = UnsupportedImportError(
            "This zip holds no files to convert - only folders, or the files a computer adds beside them."
        )
        return []
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        zip_row.refusal = ImportTooLargeError(
            f"This zip holds {len(infos)} files, and at most {MAX_ARCHIVE_MEMBERS} are read from one zip. Split it "
            "and import the parts."
        )
        return []
    zip_row.opened = len(infos)
    rows = []
    for info in infos:
        row = BatchRow(part=zip_row.part, name=info.filename, size=info.file_size, sha256="", _info=info, _zip=zip_row)
        row._archive = archive
        try:
            with archive.open(info) as member:
                head = member.read(divejson.SNIFF_BYTES)
        except _MEMBER_READ_FAILURES:
            row.refusal = _unreadable_member()
            rows.append(row)
            continue
        if head.startswith(_ZIP_MAGIC):
            row.kind = FileKind.ZIP
            row.format = "zip"
            row.refusal = UnsupportedImportError(
                "This is a zip inside a zip, which is not opened. Import it on its own."
            )
        else:
            _classify(row, head, partial(archive.open, info))
        rows.append(row)
    return rows


def _unreadable_member() -> MalformedImportError:
    return MalformedImportError(
        "This file could not be read out of its zip. If the zip is password-protected, extract it first and import "
        "the files on their own; otherwise the zip is damaged."
    )


def _digest_member(row: BatchRow) -> None:
    """A zip member's own SHA-256, read off the member in chunks."""
    assert row._archive is not None and row._info is not None
    hasher = hashlib.sha256()
    try:
        with row._archive.open(row._info) as member:
            for chunk in iter(lambda: member.read(_READ_CHUNK_SIZE), b""):
                hasher.update(chunk)
    except _MEMBER_READ_FAILURES:
        row.refusal = row.refusal or _unreadable_member()
        row._source = None
    row.sha256 = hasher.hexdigest()


def _planned_size(batch: LoadedBatch) -> int:
    """What the batch would parse into memory: every file a reader claims or that claims to
    be a document, a zip's by its declared sizes, and the archive's own document."""
    total = 0
    for row in batch.rows:
        if row.refusal is not None:
            continue
        if row.kind in (FileKind.DOCUMENT, FileKind.LOGBOOK, FileKind.COMPUTER):
            total += row.size
        elif row.kind is FileKind.ARCHIVE and row._archive is not None:
            total += row._archive.getinfo(DIVEJSON_NAME).file_size
    return total


def _survey(parts: Sequence[ImportPart]) -> LoadedBatch:
    """Classify every part and every file a zip holds, bound the batch, and order it.

    Reads heads, directories and digests, and converts nothing: the planning bound is checked
    before any file is read whole, so an import too large to plan costs no conversion.
    """
    batch = LoadedBatch()
    try:
        for part in parts:
            if part.refusal is not None or part.spool is None:
                batch.rows.append(
                    BatchRow(
                        part=part.index, name=part.filename, size=part.size, sha256=part.sha256, refusal=part.refusal
                    )
                )
                continue
            if part.size == 0 or _hidden(part.filename):
                continue
            spool = part.spool
            spool.seek(0)
            head = spool.read(divejson.SNIFF_BYTES)
            spool.seek(0)
            row = BatchRow(part=part.index, name=part.filename, size=part.size, sha256=part.sha256)
            batch.rows.append(row)
            if not head.startswith(_ZIP_MAGIC):
                _classify(row, head, partial(_rewound, spool))
                continue
            row.kind, row.format = FileKind.ZIP, "zip"
            try:
                archive = _open_archive(spool)
            except (MalformedImportError, ImportTooLargeError) as exc:
                row.refusal = exc
                continue
            batch._containers.append(archive)
            row._archive = archive
            if _holds(archive, DIVEJSON_NAME):
                row.kind, row.format = FileKind.ARCHIVE, "archive"
            else:
                batch.rows.extend(_open_zip(archive, row))

        planned = _planned_size(batch)
        if planned > MAX_DOCUMENT_SIZE:
            # Rounded *up*: floored, a batch one byte over the cap reports the cap back at
            # itself and reads as a refusal for no reason.
            raise ImportTooLargeError(
                f"This import holds {-(-planned // (1024 * 1024))} MB of logbooks uncompressed, and at most "
                f"{MAX_DOCUMENT_SIZE // (1024 * 1024)} MB are converted in one import. Split it and import the parts."
            )

        for row in batch.rows:
            if row._info is not None:
                _digest_member(row)
        batch.rows.sort(key=lambda row: (row.kind, row.name, row.sha256))
        index_of = {id(row): index for index, row in enumerate(batch.rows)}
        for row in batch.rows:
            if row._zip is not None:
                row.container = index_of[id(row._zip)]
        return batch
    except BaseException:
        batch.close()
        raise


async def _load(row: BatchRow, *, archive_read: bool) -> LoadedImport:
    """One classified file as a document: parsed, or converted, in a thread."""
    if row.kind is FileKind.ARCHIVE:
        if archive_read:
            raise UnsupportedImportError(
                "This is a second full-export archive, and one import restores one. Import it on its own."
            )
        assert row._archive is not None
        return await run_in_threadpool(_load_archive, row._archive, digest=row.sha256)
    source = row._source
    assert source is not None
    if row.kind is FileKind.DOCUMENT:
        return await run_in_threadpool(_load_document, lambda: _read_all(source), digest=row.sha256)
    assert row.format is not None
    return await run_in_threadpool(
        _convert_file, source, digest=row.sha256, name=row.name, claimed=row.format, size=row.size
    )


async def load_import(parts: Sequence[ImportPart]) -> LoadedBatch:
    """Read a request's parts into one document per file, in the batch's order.

    A file that does not read is a row with its refusal and stops nothing else. An import in
    which no file reads raises what its first refused file would raise on its own - so a
    request of one file answers as that file always has - and one whose every part was
    packaging or empty is a file nothing here reads.

    The caller owns the result and must close it - `with await load_import(...) as batch` -
    which releases the containers it opened.
    """
    batch = await run_in_threadpool(_survey, parts)
    try:
        archive_read = False
        for row in batch.rows:
            if row.refusal is not None or row.kind in (FileKind.ZIP, FileKind.UNREAD):
                continue
            try:
                row.loaded = await _load(row, archive_read=archive_read)
            except (UnsupportedImportError, MalformedImportError, ImportTooLargeError) as exc:
                row.refusal = exc
                continue
            archive_read = archive_read or row.kind is FileKind.ARCHIVE
        if not batch.read:
            raise next((row.refusal for row in batch.rows if row.refusal is not None), None) or _unrecognized()
        return batch
    except BaseException:
        batch.close()
        raise
