"""Getting an upload as far as a parsed DiveJSON document, and no further.

Two shapes arrive here and one comes out: a bare `.divejson` document, or the archive
`GET /export/archive` produces, whose `logbook.divejson` member is the same document with
the stored binaries beside it. Everything downstream works on `ImportDocument` plus a way
to fetch those binaries, so the container is this module's problem alone.

**Spooled, never buffered.** The upload is read in bounded chunks into a
`SpooledTemporaryFile` - the export path's own pattern, in the other direction - because
half a gigabyte resident per in-flight request is what `SPOOL_THRESHOLD` exists to avoid.
The recorded limit of that: parsing the JSON itself still materializes the whole document,
so the *document* cap rather than the archive one is the real memory ceiling, and a
logbook large enough to matter wants a streaming parser or an arq job. `DECISIONS.md`,
*"Logbook import spools its upload and still parses the document whole"*, carries the
trade.

The caps are new constants rather than a reuse of any existing upload limit: the largest
one this app has is the 10 MB card scan, and a logbook with a thousand sampled dives is
two orders of magnitude past it.
"""

import hashlib
import json
import logging
import tempfile
import zipfile
from dataclasses import dataclass
from typing import IO, Any

from fastapi import UploadFile
from pydantic import ValidationError

from ...schemas.export import DIVEJSON_FORMAT, DIVEJSON_VERSION
from ...schemas.logbook_import import ImportDocument
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
    """The upload is not a DiveJSON document this reader implements - a 415.

    Deliberately the same distinction `POST /dive/parse` draws: 415 is "this is not a file
    I can read", 422 is "this is one, and it is broken". The two land in different places
    in a client, and collapsing them would tell a diver their export is corrupt when they
    have handed over a photo.
    """


class MalformedImportError(Exception):
    """The upload is a DiveJSON document and cannot be read - a 422."""


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

    Member order survives into the parsed dict, which is what lets a caller check the
    `format`/`version` rule without re-reading the text.

    `tests/helpers/divejson.py` re-exports this rather than carrying its own copy: it is
    the one piece of that validator port with a reader on the request path, and two
    spellings of "reject a repeated member" is precisely the two-shapes-for-one-fact
    problem the format's own supersession decision rejects.
    """
    return json.loads(text, object_pairs_hook=_reject_duplicate_members)


@dataclass(slots=True)
class LoadedImport:
    """A parsed document plus, on the archive path, the container its binaries live in.

    Holds an open temp file and possibly an open `ZipFile`, so it is a context manager and
    the caller must use it as one. The spool deletes itself on close, exactly as the
    export path's does.
    """

    document: ImportDocument
    digest: str
    is_archive: bool
    _spool: IO[bytes]
    _archive: zipfile.ZipFile | None

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


def _document_bytes(buffer: IO[bytes], archive: zipfile.ZipFile | None) -> bytes:
    if archive is None:
        return buffer.read()
    try:
        info = archive.getinfo(DIVEJSON_NAME)
    except KeyError:
        raise UnsupportedImportError(
            f"This archive has no {DIVEJSON_NAME} member, so there is no logbook in it to import."
        ) from None
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
        raise UnsupportedImportError("This file is not a DiveJSON document.")
    if raw.get("format") != DIVEJSON_FORMAT:
        raise UnsupportedImportError("This file is not a DiveJSON document - its `format` member says otherwise.")

    version = raw.get("version")
    if not isinstance(version, str) or version.split(".", 1)[0] != _SUPPORTED_MAJOR:
        raise UnsupportedImportError(
            f"This document declares DiveJSON version {version!r}, and this app implements {DIVEJSON_VERSION}. "
            "Minor versions are additive and are read; a different major version is not."
        )

    # **Member order is deliberately not checked here**, and it is the one §3 rule with an
    # obvious place to check it: `format` and `version` MUST be a document's first two
    # members (spec §4), the parse preserves that order, and refusing anything else would
    # be four lines. It is a *writer's* obligation, `divejson validate` is where it is
    # enforced, and refusing an otherwise perfectly readable logbook over the order two
    # members were written in is the failure this feature exists to end. Same reasoning as
    # the dangling reference the planner reports rather than rejects.

    try:
        return ImportDocument.model_validate(raw)
    except ValidationError as exc:
        raise MalformedImportError(_first_error(exc)) from exc


def _first_error(exc: ValidationError) -> str:
    """One sentence naming where the document broke.

    The first error rather than all of them: a structurally wrong document produces one
    per record, and a diver needs the location more than the count.
    """
    errors = exc.errors()
    if not errors:
        return "This DiveJSON document could not be read."
    first = errors[0]
    location = ".".join(str(part) for part in first["loc"]) or "the document"
    return f"This DiveJSON document could not be read: {location}: {first['msg']}."


async def load_import(upload: UploadFile) -> LoadedImport:
    """Read an upload into a parsed document, spooling as it goes.

    The caller owns the result and must close it - `with load_import(...) as loaded` -
    which is what deletes the spool and, on the archive path, releases the container.
    """
    buffer = await _spool_upload(upload, MAX_ARCHIVE_SIZE)
    archive: zipfile.ZipFile | None = None
    try:
        digest = _digest(buffer)
        is_archive = buffer.read(len(_ZIP_MAGIC)) == _ZIP_MAGIC
        buffer.seek(0)

        if is_archive:
            archive = _open_archive(buffer)
        elif buffer.seek(0, 2) > MAX_DOCUMENT_SIZE:
            # The upload was admitted against the archive cap because nothing said which
            # shape it was until now. A bare document gets the smaller one.
            raise ImportTooLargeError(
                f"A DiveJSON document may be up to {MAX_DOCUMENT_SIZE // (1024 * 1024)} MB. "
                "Import the archive if you are restoring a whole account with its files."
            )
        buffer.seek(0)

        raw_bytes = _document_bytes(buffer, archive)
        try:
            raw = parse_document(raw_bytes)
        except DuplicateMemberError:
            raise
        except UnicodeDecodeError as exc:
            raise UnsupportedImportError("This file is not a DiveJSON document - it is not UTF-8 text.") from exc
        except json.JSONDecodeError as exc:
            raise MalformedImportError(f"This DiveJSON document is not valid JSON: {exc.msg} at line {exc.lineno}.")

        return LoadedImport(
            document=_validate_envelope(raw),
            digest=digest,
            is_archive=archive is not None,
            _spool=buffer,
            _archive=archive,
        )
    except BaseException:
        if archive is not None:
            archive.close()
        buffer.close()
        raise
