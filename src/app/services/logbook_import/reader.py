"""Getting an upload as far as a parsed DiveJSON document, and no further.

Four shapes arrive here and one comes out: a bare `.divejson` document; the archive
`GET /export/archive` produces, whose `logbook.divejson` member is the same document with
the stored binaries beside it; a logbook in any format the `divejson` converter reads; and
a zip whose members are all one of those formats, which is one logbook. Everything
downstream works on `ImportDocument` plus a way to fetch the archive's binaries, so both
the container and the conversion are this module's problem alone.

**The converter is consulted once, on a bounded head, before the JSON parse.** Not at the
refusal sites further down: an `.ssrf` or a UDDF upload is valid UTF-8 that is not JSON and
would die in `parse_document` before any of them was reached, told that a file which never
claimed to be DiveJSON is a broken DiveJSON document. So `load_import` reads
`divejson.SNIFF_BYTES` off the spool and asks `divejson.sniff` - which takes *bytes* and
reads nothing itself - right after the zip decision. A named format is converted; `None`
means nothing claimed the bytes, and the DiveJSON path below is untouched for anything that
parses as JSON.

**A zip is this module's to recognise and the library's to read.** The `PK\\x03\\x04` sniff
and `_open_archive`'s declared-size guard stay here, because they are what keeps a zip bomb
off the disk; a container carrying `logbook.divejson` is the app's own export, and one
without goes to the converter whole, under this module's own caps.

**Spooled, never buffered, and converted in a thread.** The upload is read in bounded chunks
into a `SpooledTemporaryFile` - the export path's own pattern, in the other direction -
because half a gigabyte resident per in-flight request is what `SPOOL_THRESHOLD` exists to
avoid. Conversion then goes through `run_in_threadpool`, as `POST /dive/parse` does with the
same decoders and for the same reason.

`MAX_DOCUMENT_SIZE` is the memory ceiling on every path, and each one reaches it
differently: a bare document is parsed whole, a named format hands the converter
`stream.read()`, and a zip of dive-computer files has the *sum* of its declared member sizes
checked against it before anything is read, because every member of one of those is
converted and every result is held until they merge. A logbook large enough to matter wants
a streaming parser or an arq job. `DECISIONS.md`, *"Logbook import spools its upload and
still parses the document whole"*, carries the trade.

The caps are new constants rather than a reuse of any existing upload limit: the largest
one this app has is the 10 MB card scan, and a logbook with a thousand sampled dives is
two orders of magnitude past it.
"""

import codecs
import hashlib
import json
import logging
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import IO, Any, BinaryIO, cast

import divejson
from divejson import Conversion, ConverterError, NonConformingOutputError, SourceTooLargeError, UnsupportedSourceError
from fastapi import UploadFile
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

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
    ImportNoteCode,
)
from ..export.archive import DIVEJSON_NAME, SPOOL_THRESHOLD

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

# A bare document. Generous against the reference implementation's own output - the demo
# logbook is kilobytes and a thousand sampled dives is tens of megabytes - and the number
# that actually bounds this endpoint's memory, since the parse holds the document whole.
MAX_DOCUMENT_SIZE = 100 * 1024 * 1024  # 100 MB

# An archive, which carries every dive-computer file and c-card scan besides. Five times
# the document cap because the binaries are what dominate it.
MAX_ARCHIVE_SIZE = 500 * 1024 * 1024  # 500 MB

# What the archive's members may sum to *uncompressed*, checked against the central
# directory before a single byte is inflated. Zip compresses text a thousand to one, so
# the transfer cap above bounds nothing on its own: without this a 5 MB upload could ask
# for gigabytes of temp file. Twice the archive cap leaves room for an honest logbook
# whose documents deflate well while refusing anything shaped like a bomb.
MAX_ARCHIVE_EXTRACTED_SIZE = 2 * MAX_ARCHIVE_SIZE

# The most files a zip of one source format may hold. This bounds the *walk* rather than the
# bytes: to decide a member's format the library has to open it and inflate a `SNIFF_BYTES`
# head - a FIT's magic sits eight bytes into the member, so no listing can answer it - and
# it does that once per member before converting any of them. An archive of half a million
# tiny files is a lot of work inside one request even when it fits under the size bound
# below. Deliberately not a promise about how many dives go in one upload:
# `_refuse_oversized_source` is what a real dive-computer export meets first, and at 30 KB a
# FIT file that is a few thousand of them. The count itself is refused off the directory
# listing, before anything is opened.
#
# What one such member may declare is `MAX_DOCUMENT_SIZE` rather than a fourth number: a
# member *is* a logbook document in some other format, materialized whole by whichever
# reader claims it, exactly as a bare document is.
MAX_ARCHIVE_MEMBERS = 5000

# Zip's local file header. Sniffed rather than trusting the filename or the client's
# content type, on the same principle as `certification_files.sniff_content_type`: the
# bytes say what they are.
_ZIP_MAGIC = b"PK\x03\x04"

_READ_CHUNK_SIZE = 1024 * 1024

# How each registered format is named to a diver. An id the library grows past this table
# falls back to the id itself, so the sentence stays true and only gets terser - but that
# tolerance is a floor, not the plan: `test_every_read_format_has_a_label` fails the build
# when the pin moves past this table, because the fallback is invisible in every other
# guard. A whole reader (`suunto_xml`, added in 0.4.0) arrived unnoticed that way, offered
# by the API and greyed out by the picker, with nothing on either side able to see it.
_FORMAT_LABELS = {
    "uddf": "UDDF (.uddf)",
    "ssrf": "Subsurface (.ssrf)",
    "fit": "FIT (.fit)",
    "suunto_json": "Suunto app JSON (.json)",
    "suunto_xml": "Suunto DM5 XML (.xml)",
}

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
    """A reader claimed the upload and could not read it - a 422."""


class ImportTooLargeError(Exception):
    """The upload is past its cap - a 413, matching `read_upload_within_limit`'s answer
    everywhere else in the app."""


class DuplicateMemberError(MalformedImportError):
    """A JSON object in the document carries the same member name twice (spec §9).

    A `MalformedImportError` because it is exactly that: `json` would otherwise keep the
    last value silently, and a document whose reader and writer disagree about which of two
    `max_depth` members is real is not something to guess at.
    """


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


@dataclass(slots=True)
class LoadedImport:
    """A parsed document plus, on the archive path, the container its binaries live in.

    Holds an open temp file and possibly an open `ZipFile`, so it is a context manager and
    the caller must use it as one. The spool deletes itself on close, exactly as the
    export path's does.

    `conversion` and `source_format` are the converter's, and both are `None` for a native
    DiveJSON upload. Nothing below this module reads either: the converted document enters
    the planner through `ImportDocument` like any other, so neither the planner nor the
    writer ever learns the logbook was not written by this app. They are here because the
    *report* is a route concern and the route has nothing else to build it from.

    `is_archive` stays "this upload carries the stored binaries", which a converted zip does
    not - the converter emits no `files` at all - so it is `False` there even though the
    upload was a container.
    """

    document: ImportDocument
    digest: str
    is_archive: bool
    conversion: Conversion | None
    source_format: str | None
    _spool: IO[bytes]
    _archive: zipfile.ZipFile | None
    # What `read_as_written` read the way a pre-change writer meant it, for the report.
    read_as_written: list[ReaderNote] = field(default_factory=list)

    def __enter__(self) -> LoadedImport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._archive is not None:
            self._archive.close()
        self._spool.close()

    def member_size(self, path: str) -> int | None:
        """The declared uncompressed size of one member, or `None` when it is not there.

        `path` is a document-supplied string and is used as a **zip member name and
        nothing else** - no filesystem call takes it, so a `../../.bashrc` in it addresses
        a member that does not exist rather than a path outside anything.

        Split from `read_member` so the *preview* can say which files would restore without
        inflating a byte of any of them, and so "not in this archive" and "too big to
        store" stay two different answers to the diver. It comes out of the central
        directory, so it is a dictionary lookup.
        """
        if self._archive is None:
            return None
        try:
            return self._archive.getinfo(path).file_size
        except KeyError:
            return None

    def read_member(self, path: str) -> bytes | None:
        """One binary out of the container, or `None` when it cannot be had.

        Unbounded on purpose: every caller has already been through `member_size` and
        refused anything over its own cap, and `_open_archive` refused the whole container
        if its declared sizes summed past `MAX_ARCHIVE_EXTRACTED_SIZE`. `zipfile` verifies
        the CRC on the way out, which is what makes those declared sizes worth trusting;
        the digest the caller then checks against the manifest is the end-to-end guarantee.

        **A member that will not inflate is `None` rather than an exception**, because the
        caller is the writer and it already has the right answer for a file it cannot have:
        skip it, report it, and leave the dive. Letting the failure out would abort a
        half-written import over one corrupt c-card scan, which is the opposite trade from
        every other file decision here.
        """
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


async def _spool_upload(upload: UploadFile, max_size: int) -> IO[bytes]:
    """Drain an upload into a spooled temp file, refusing it past `max_size`.

    `read_upload_within_limit` is the wrong tool at these sizes - it returns `bytes`, and
    the whole point here is that half a gigabyte never becomes a Python object.
    """
    buffer: IO[bytes] = tempfile.SpooledTemporaryFile(max_size=SPOOL_THRESHOLD)
    total = 0
    try:
        while chunk := await upload.read(_READ_CHUNK_SIZE):
            total += len(chunk)
            if total > max_size:
                limit_mb = -(-max_size // (1024 * 1024))
                raise ImportTooLargeError(f"File too large. Maximum allowed size is {limit_mb} MB.")
            buffer.write(chunk)
    except BaseException:
        buffer.close()
        raise
    buffer.seek(0)
    return buffer


def _digest(buffer: IO[bytes]) -> str:
    """The upload's own sha256, which is what the preview token is minted over.

    Read back off the spool in chunks rather than hashed on the way in, so the hash and the
    stored bytes cannot disagree: whatever `apply` is handed is hashed the same way.
    """
    hasher = hashlib.sha256()
    buffer.seek(0)
    for chunk in iter(lambda: buffer.read(_READ_CHUNK_SIZE), b""):
        hasher.update(chunk)
    buffer.seek(0)
    return hasher.hexdigest()


def _logbook_member(archive: zipfile.ZipFile) -> zipfile.ZipInfo | None:
    """The archive's `logbook.divejson`, or `None` when it has none.

    `None` is not a refusal any more: a zip without one is a zip of dive-computer files,
    which the converter reads as one logbook. It used to be the first of this module's five
    415s.
    """
    try:
        return archive.getinfo(DIVEJSON_NAME)
    except KeyError:
        return None


def _document_bytes(buffer: IO[bytes], archive: zipfile.ZipFile | None, info: zipfile.ZipInfo | None) -> bytes:
    if archive is None or info is None:
        return buffer.read()
    if info.file_size > MAX_DOCUMENT_SIZE:
        raise ImportTooLargeError(
            f"The {DIVEJSON_NAME} inside this archive is larger than the {MAX_DOCUMENT_SIZE // (1024 * 1024)} MB limit."
        )
    try:
        return archive.read(info)
    except _MEMBER_READ_FAILURES as exc:
        # A 422 rather than a 415: this *is* an archive of the shape this app produces, and
        # the logbook inside it cannot be read. The message names the two causes a diver can
        # do something about, because "could not be read" on its own sends nobody anywhere.
        raise MalformedImportError(
            f"The {DIVEJSON_NAME} inside this archive could not be read. If the archive is password-protected, "
            "extract it first and import the document on its own; otherwise the file is damaged."
        ) from exc


def _refuse_oversized_source(archive: zipfile.ZipFile) -> None:
    """The whole conversion's memory ceiling, off the central directory before anything runs.

    `MAX_ARCHIVE_EXTRACTED_SIZE` is not this bound and never was: it guards the *export
    archive*, whose members are written to the file store one at a time and whose only
    resident object is the `logbook.divejson` under `MAX_DOCUMENT_SIZE`. A zip of
    dive-computer files is the opposite shape - every member is converted and every member's
    result is held until the merge - so without this the ceiling would be the 1 GB the
    zip-bomb guard admits, ten times the number this module's own docstring calls the
    ceiling, and reached by an upload well under `MAX_ARCHIVE_SIZE` because XML deflates
    about ten to one. `max_member_size` bounds one member and `MAX_ARCHIVE_MEMBERS` bounds
    the count; neither bounds the sum, which is the thing that ends up in memory.

    `MAX_DOCUMENT_SIZE` rather than a fourth number, for the same reason a member gets it:
    whatever shape a logbook arrives in, at most a document's worth of source becomes one
    in-memory logbook. Every entry counts, including the directory entries and the `__MACOSX`
    tree the converter skips - a bound slightly stricter than the set actually read is the
    safe direction, and matching the library's member filter here would be a second copy of
    a rule that lives there.
    """
    declared = sum(info.file_size for info in archive.infolist())
    if declared > MAX_DOCUMENT_SIZE:
        # Rounded *up*, as `_spool_upload` rounds its own: floored, an archive one byte over
        # the cap reports the cap back at itself and reads as a refusal for no reason.
        raise ImportTooLargeError(
            f"This archive holds {-(-declared // (1024 * 1024))} MB of logbooks uncompressed, and at most "
            f"{MAX_DOCUMENT_SIZE // (1024 * 1024)} MB are converted in one import. Split it and import the parts."
        )


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


def read_as_written(raw: dict[str, Any]) -> list[ReaderNote]:
    """Read a document a known writer produced before the format moved, as that writer meant it.

    Rewrites `raw` in place into the current shape and says what it read, one report line
    per kind: the profile axis in seconds, multiplied; the readouts on the dive, onto its
    first recording - minting one where the dive has none, a recording of readouts alone;
    `water_type: "en13319"` onto that recording's `salinity`, dropped where the dive has
    neither a recording nor a readout to carry it; and a cylinder's `po2_limit` as
    `ppo2_limit`. Anything else passes untouched, and a document no known writer produced is
    not looked at: without this every old spelling would vanish silently, the importer
    ignoring what it does not know (§5.6).
    """
    if not _written_before_the_axis_moved(raw):
        return []

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
    return summary + notes


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


def formats_this_build_reads() -> str:
    """The registry's read formats, as a diver would name them.

    Derived from `divejson.read_formats()` on every call rather than written out: the pin
    moves on its own, and a sentence listing four formats while the build reads five is the
    one failure a message like this can have.
    """
    return ", ".join(_FORMAT_LABELS.get(fmt, fmt) for fmt in divejson.read_formats())


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


def _convert(buffer: IO[bytes], *, source_format: str | None) -> Conversion:
    """Hand the spool to the converter, rewound, with the caps on the branch that reads them.

    **The rewind is not optional.** `registry.convert` reads its own sniff head from the
    stream's *current* position and seeks back only on the `format=None` branch, so a spool
    left where this module's own sniff put it would feed the archive walker bytes 8192
    onward - which sniffs `None` and turns a perfectly good zip into a 415 - and would hand
    a named reader its file minus the first 8 KB.

    **The caps go with `format=None` and nowhere else.** As shipped, a named format converts
    `stream.read()` from the current position and never consults `max_members` or
    `max_member_size`; passing them there would look like a guard and be inert. On the branch
    that does read them, `max_member_size` is belt to `_refuse_oversized_source`'s braces -
    the sum is already bounded by the same number, so no single member can exceed it - and it
    stays because a bound inside the library is the one that still holds if this module ever
    hands over a container it did not open itself.

    `exported_at` is passed rather than defaulted because the library's default is *now, in
    the local zone*, and this is the one value in a converted document that is not a
    function of the source. Preview and apply convert the same bytes minutes apart and must
    plan identically, so nothing downstream reads it - `divejson.compared` drops it, and the
    planner never looks.
    """
    buffer.seek(0)
    exported_at = _conversion_moment()
    # `convert` is annotated `BinaryIO`, which differs from the `IO[bytes]` this module
    # spools into only in what `__enter__` returns - and `convert` never enters it. It reads
    # and seeks, both of which a `SpooledTemporaryFile` does.
    stream = cast(BinaryIO, buffer)
    if source_format is not None:
        return divejson.convert(stream, format=source_format, exported_at=exported_at)
    return divejson.convert(
        stream,
        exported_at=exported_at,
        max_members=MAX_ARCHIVE_MEMBERS,
        max_member_size=MAX_DOCUMENT_SIZE,
    )


def _converted_from(document: Any, fallback: str | None) -> str | None:
    """Which format the converted document says it came from.

    On the archive path this is the only place the answer exists: the api decided "zip" and
    the library decided which reader every member named. `converting.md`'s *Provenance*
    block is where it records that, and a member the merge dropped leaves the fallback -
    what this module sniffed - which is `None` for an archive and honest either way.
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


async def _convert_source(
    buffer: IO[bytes], *, source_format: str | None
) -> tuple[ImportDocument, Conversion, str | None]:
    """Convert the spool and take the result through `_validate_envelope` like any upload.

    **In a thread, never on the event loop.** Reading a FIT file is the same pure-Python
    decode `POST /dive/parse` hands to `run_in_threadpool` at about two seconds a megabyte,
    and a zip of them is that many times over - inline in an `async def` one upload stalls
    every other request on the worker, `/health/ready` included. `DECISIONS.md`, *"Uploaded
    files are parsed in a thread, not on the event loop"*, is the rule and this is the same
    work.

    Every refusal below is the converter's, translated into this module's three so the route
    keeps one taxonomy. The last arm is the one that matters: a `ConverterError` this build
    has never seen still lands as a 422 rather than escaping as a 500, and the registry may
    grow one at any pin bump.
    """
    try:
        conversion = await run_in_threadpool(_convert, buffer, source_format=source_format)
    except SourceTooLargeError as exc:
        # The converter's own sentence again, and for the same reason as the 415 below: it
        # names which of its bounds fired, and restating them here would let this message
        # claim a cause that cannot be one. `max_member_size` is the case in point - the sum
        # check upstream already bounds it, so a per-member refusal is unreachable, and a
        # message asserting it would send a diver looking for one huge file.
        #
        # **And this is not an archive-only refusal**, which the container framing hid: an
        # adapter sets caps of its own on the work one already-bounded file may ask for - the
        # FIT reader refuses past a hundred thousand messages - and that reaches here on the
        # named-format path, where there is no container and nothing to split. `source_format`
        # is exactly the difference, so the advice goes with the branch that can act on it.
        if source_format is None:
            raise ImportTooLargeError(
                f"This archive is past what one import reads - {exc}. Split it and import the parts."
            ) from exc
        raise ImportTooLargeError(f"This file is past what one import reads - {exc}.") from exc
    except UnsupportedSourceError as exc:
        # The converter's own sentence, prefixed rather than replaced. It reaches here for
        # three container cases - an empty archive, a member no reader claims, an archive
        # mixing two formats - and the api cannot tell them apart from the exception type,
        # while each of the three says something more useful than a list of formats would.
        # The one that *wants* the list carries the registry's own names already, which is
        # why this deliberately does not append `formats_this_build_reads()`: the same list
        # twice, spelled two ways, is worse than the library's spelling of it once.
        raise UnsupportedImportError(f"This archive is not one logbook this app can read - {exc}.") from exc
    except NonConformingOutputError as exc:
        # A converter bug, not a diver's file: the library validates its own output and this
        # is it saying no. Logged with a traceback because nobody else will see it, and 422
        # rather than 500 because the registry backstop is that this endpoint does not 500.
        logger.exception("The converter produced a non-conforming document during a logbook import")
        raise MalformedImportError(_CONVERTER_BUG) from exc
    except ConverterError as exc:
        raise MalformedImportError(f"This logbook could not be converted: {exc}.") from exc

    try:
        document = _validate_envelope(conversion.document)
    except (UnsupportedImportError, MalformedImportError) as exc:
        logger.exception("A converted document did not survive this app's own envelope check")
        raise MalformedImportError(_CONVERTER_BUG) from exc
    return document, conversion, _converted_from(conversion.document, source_format)


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


def conversion_report(loaded: LoadedImport) -> ConversionReport | None:
    """What the conversion could not carry, or `None` for a native DiveJSON upload.

    Built here rather than in the browser so preview and result render one shape from one
    grouping, and built from `Conversion.grouped()` rather than by regrouping the notes,
    because the library's grouping is the one the CLI prints and two of them would drift.
    """
    if loaded.conversion is None:
        return None
    groups = loaded.conversion.grouped()
    return ConversionReport(
        format=loaded.source_format or _UNKNOWN_SOURCE_FORMAT,
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


async def load_import(upload: UploadFile) -> LoadedImport:
    """Read an upload into a parsed document, converting it first where it needs it.

    The caller owns the result and must close it - `with load_import(...) as loaded` -
    which is what deletes the spool and, on the archive path, releases the container.
    """
    buffer = await _spool_upload(upload, MAX_ARCHIVE_SIZE)
    archive: zipfile.ZipFile | None = None
    try:
        digest = _digest(buffer)
        head = buffer.read(divejson.SNIFF_BYTES)
        buffer.seek(0)

        logbook: zipfile.ZipInfo | None = None
        if head.startswith(_ZIP_MAGIC):
            # Opened here, and by this module's guard, before the converter is given the
            # same spool: `_open_archive` is what refuses a zip bomb off the central
            # directory, and the library's per-member caps are a second bound rather than a
            # replacement for it.
            archive = _open_archive(buffer)
            logbook = _logbook_member(archive)
            if logbook is None:
                # Not this app's export archive: a zip of dive-computer files, which is one
                # logbook. The container is the library's to walk - a FIT's magic sits eight
                # bytes into the *member*, so no bounded head of the zip could decide it here.
                _refuse_oversized_source(archive)
                archive.close()
                archive = None
                document, conversion, source_format = await _convert_source(buffer, source_format=None)
                return LoadedImport(
                    document=document,
                    digest=digest,
                    is_archive=False,
                    conversion=conversion,
                    source_format=source_format,
                    _spool=buffer,
                    _archive=None,
                )
        elif buffer.seek(0, 2) > MAX_DOCUMENT_SIZE:
            # The upload was admitted against the archive cap because nothing said which
            # shape it was until now. Anything but a container gets the smaller one.
            raise ImportTooLargeError(
                f"A logbook document may be up to {MAX_DOCUMENT_SIZE // (1024 * 1024)} MB. "
                "Import the full-export archive if you are restoring a whole account with its files."
            )
        buffer.seek(0)

        if archive is None:
            # `sniff` also answers `zip`, which is a container marker rather than a reader
            # anyone can ask for - and this branch is the one where the bytes were not a
            # zip anyway. Asking `read_formats()` rather than excluding that one value is
            # what keeps this true when the registry grows another container.
            claimed = divejson.sniff(head)
            if claimed is not None and claimed in divejson.read_formats():
                document, conversion, source_format = await _convert_source(buffer, source_format=claimed)
                return LoadedImport(
                    document=document,
                    digest=digest,
                    is_archive=False,
                    conversion=conversion,
                    source_format=source_format,
                    _spool=buffer,
                    _archive=None,
                )
            buffer.seek(0)

        raw_bytes = _document_bytes(buffer, archive, logbook)
        try:
            raw = parse_document(raw_bytes)
        except DuplicateMemberError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            # Nothing claimed these bytes and they are not JSON. A head that opens `{` is a
            # file that claimed to be a document, so it keeps the 422 it has always had;
            # anything else - a `.txt`, a CSV, a photo - never claimed to be one, and being
            # told it is a broken DiveJSON document is the wrong sentence. Read off the
            # *document's* head rather than the upload's, which on the archive path is the
            # zip's; an archive that carries a `logbook.divejson` claimed to be this app's
            # export whatever is inside it, so that path keeps the 422 unconditionally.
            if archive is None and not _claims_to_be_json(raw_bytes[: divejson.SNIFF_BYTES]):
                raise _unrecognized() from exc
            if isinstance(exc, UnicodeDecodeError):
                raise MalformedImportError("This logbook document is not valid JSON: it is not UTF-8 text.") from exc
            raise MalformedImportError(f"This logbook document is not valid JSON: {exc.msg} at line {exc.lineno}.")

        read = read_as_written(raw) if isinstance(raw, dict) else []
        return LoadedImport(
            document=_validate_envelope(raw),
            digest=digest,
            is_archive=archive is not None,
            conversion=None,
            source_format=None,
            _spool=buffer,
            _archive=archive,
            read_as_written=read,
        )
    except BaseException:
        if archive is not None:
            archive.close()
        buffer.close()
        raise
