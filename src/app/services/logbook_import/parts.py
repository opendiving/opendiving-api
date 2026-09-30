"""An import's request body: every `file` part spooled once, under one bound on memory.

The two import routes read their own body rather than declaring `File` and `Form`
parameters, because the framework's form reader gives none of the three properties an
import of many files needs. It reads the whole body before any dependency runs, so a request
with no session spools its upload before it is refused; it holds every file part of up to
1 MiB in memory however many there are; and it bounds neither one file part nor the total.
Here the caller is authenticated before a byte is read, and while the body streams:

- the bytes of every part held in memory together never pass one `SPOOL_THRESHOLD` - a part
  that would take the total past it rolls onto disk first;
- the request stops at `MAX_ARCHIVE_SIZE` over every part, and at `MAX_PARTS` file parts,
  each a 413 that says what to do;
- a part past its own bound - `MAX_ARCHIVE_SIZE` for a zip, `MAX_DOCUMENT_SIZE` for
  anything else, the figures one upload has always had - stops being kept and becomes a
  refused row, while its digest is still taken, since the token covers every part sent.

`DECISIONS.md`, *"Logbook import spools its upload and still parses the document whole"*,
has the trade this sits inside.
"""

import hashlib
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from tempfile import SpooledTemporaryFile
from typing import IO, Any

from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from ..export.archive import SPOOL_THRESHOLD
from . import reader
from .reader import ImportTooLargeError

# The field every file of an import arrives under, once per file.
FILE_FIELD = "file"

# The most file parts one request may carry: the figure the framework's own form reader
# refuses at, answered as this api's 413 with a sentence rather than the framework's 400.
MAX_PARTS = 1000

# What a text field may hold. A token, a check-in submission and a portrait choice are each
# well under a kilobyte; this is the framework's own figure for a field.
_MAX_FIELD_SIZE = 1024 * 1024

_ZIP_MAGIC = b"PK\x03\x04"


class MalformedRequestError(Exception):
    """The body is not multipart form data this reader can walk - a 400, as the framework
    answers it."""


@dataclass(slots=True)
class ImportPart:
    """One `file` part of the request, spooled, with its digest taken on the way in.

    `spool` is `None` for a part refused while it streamed, whose `refusal` then says why;
    its `size` and `sha256` are still the whole part's. `index` is its position among the
    request's file parts, which is how a report row names the file a diver picked.
    """

    index: int
    filename: str
    size: int
    sha256: str
    spool: IO[bytes] | None
    refusal: ImportTooLargeError | None = None


@dataclass(slots=True)
class ImportRequest:
    """The parts and the named text fields of one import request. Closes its spools."""

    parts: list[ImportPart]
    fields: dict[str, str]

    def __enter__(self) -> ImportRequest:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        for part in self.parts:
            if part.spool is not None:
                part.spool.close()


def batch_digest(parts: Sequence[ImportPart]) -> str:
    """What the preview token is minted over: every part's name and digest.

    Ordered by name and then digest, so the order a browser's folder walk sends files in -
    which is not sorted - changes nothing, and taken over what the request carried rather
    than over what the import made of it, so an apply whose set of files differs is refused
    before a byte of it is converted.
    """
    hasher = hashlib.sha256()
    for part in sorted(parts, key=lambda one: (one.filename, one.sha256)):
        hasher.update(f"{part.filename}\x00{part.sha256}\n".encode())
    return hasher.hexdigest()


def _mb(size: int) -> int:
    """A byte bound in whole megabytes, rounded up so a figure never reads below its bound."""
    return -(-size // (1024 * 1024))


@dataclass(slots=True)
class _FilePart:
    """A part being streamed: where its bytes go, and what is known of them so far."""

    index: int
    filename: str
    hasher: Any = field(default_factory=hashlib.sha256)
    size: int = 0
    head: bytes = b""
    spool: SpooledTemporaryFile[bytes] | None = field(default_factory=lambda: SpooledTemporaryFile(max_size=0))
    in_memory: int = 0
    rolled: bool = False
    refusal: ImportTooLargeError | None = None


@dataclass(slots=True)
class _Field:
    name: str
    data: bytearray = field(default_factory=bytearray)


class _BodyReader:
    """The parser's callbacks, and the bounds they keep.

    The callbacks are synchronous and decide everything; the writes they queue are made
    between chunks, so a spool on disk is written off the event loop.
    """

    def __init__(self, fields: Collection[str]) -> None:
        self._wanted = frozenset(fields)
        self._charset = "utf-8"
        self.parts: list[_FilePart] = []
        self.fields: dict[str, str] = {}
        self._current: _FilePart | _Field | None = None
        self._header_name = b""
        self._header_value = b""
        self._disposition = b""
        self._total = 0
        self._in_memory = 0
        self._pending: list[tuple[_FilePart, bytes]] = []

    def _decoded(self, data: bytes) -> str:
        """Text in the request's charset, and Latin-1 where that charset cannot decode text at
        all - an unknown name, a codec that is not a text encoding - which is the framework's
        own fallback: Latin-1 names every byte, so a bogus charset reads rather than fails."""
        try:
            return data.decode(self._charset, errors="replace")
        except LookupError, UnicodeError:
            return data.decode("latin-1")

    # ------------------------------------------------------------------ callbacks

    def on_part_begin(self) -> None:
        self._current = None
        self._disposition = b""

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_name += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def on_header_end(self) -> None:
        if self._header_name.lower() == b"content-disposition":
            self._disposition = self._header_value
        self._header_name = b""
        self._header_value = b""

    def on_headers_finished(self) -> None:
        _, options = parse_options_header(self._disposition)
        name = options.get(b"name")
        if name is None:
            raise MalformedRequestError('The Content-Disposition header field "name" must be provided.')
        field_name = self._decoded(name)
        if field_name == FILE_FIELD:
            if len(self.parts) >= MAX_PARTS:
                raise ImportTooLargeError(
                    f"This import carries more than {MAX_PARTS} files. Zip them and import the zip instead."
                )
            filename = self._decoded(options.get(b"filename", b""))
            part = _FilePart(index=len(self.parts), filename=filename)
            self.parts.append(part)
            self._current = part
        elif field_name in self._wanted:
            self._current = _Field(field_name)

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        self._total += len(chunk)
        if self._total > reader.MAX_ARCHIVE_SIZE:
            raise ImportTooLargeError(
                f"This import is larger than the {_mb(reader.MAX_ARCHIVE_SIZE)} MB one import may carry. "
                "Import it in parts."
            )
        current = self._current
        if isinstance(current, _Field):
            if len(current.data) + len(chunk) > _MAX_FIELD_SIZE:
                raise MalformedRequestError(f"The {current.name} field is larger than {_MAX_FIELD_SIZE // 1024} KB.")
            current.data.extend(chunk)
        elif isinstance(current, _FilePart):
            self._take(current, chunk)

    def on_part_end(self) -> None:
        if isinstance(self._current, _Field):
            self.fields[self._current.name] = self._decoded(bytes(self._current.data))
        self._current = None

    # ------------------------------------------------------------------ file parts

    def _take(self, part: _FilePart, chunk: bytes) -> None:
        part.hasher.update(chunk)
        part.size += len(chunk)
        if len(part.head) < len(_ZIP_MAGIC):
            part.head += chunk[: len(_ZIP_MAGIC) - len(part.head)]
        if part.spool is None:
            return
        is_zip = part.head.startswith(_ZIP_MAGIC)
        bound = reader.MAX_ARCHIVE_SIZE if is_zip else reader.MAX_DOCUMENT_SIZE
        if part.size > bound:
            part.refusal = ImportTooLargeError(
                f"A logbook document may be up to {_mb(reader.MAX_DOCUMENT_SIZE)} MB. Import the full-export "
                "archive if you are restoring a whole account with its files."
            )
            self._release(part)
            return
        self._pending.append((part, chunk))

    def _release(self, part: _FilePart) -> None:
        if part.spool is not None:
            part.spool.close()
            part.spool = None
        self._in_memory -= part.in_memory
        part.in_memory = 0

    async def flush(self) -> None:
        """Write what the last chunk queued: in memory while the whole request's held bytes
        stay under one `SPOOL_THRESHOLD`, on disk - rolled over first - once they would not."""
        pending, self._pending = self._pending, []
        for part, chunk in pending:
            spool = part.spool
            if spool is None:
                continue
            if not part.rolled and self._in_memory + len(chunk) > SPOOL_THRESHOLD:
                await run_in_threadpool(spool.rollover)
                part.rolled = True
                self._in_memory -= part.in_memory
                part.in_memory = 0
            if part.rolled:
                await run_in_threadpool(spool.write, chunk)
            else:
                spool.write(chunk)
                part.in_memory += len(chunk)
                self._in_memory += len(chunk)

    def close(self) -> None:
        for part in self.parts:
            self._release(part)

    def finished(self) -> list[ImportPart]:
        finished = []
        for part in self.parts:
            if part.spool is not None:
                part.spool.seek(0)
            finished.append(
                ImportPart(
                    index=part.index,
                    filename=part.filename,
                    size=part.size,
                    sha256=part.hasher.hexdigest(),
                    spool=part.spool,
                    refusal=part.refusal,
                )
            )
        return finished


async def read_import_request(request: Request, *, fields: Collection[str] = ()) -> ImportRequest:
    """Stream the request's body into its `file` parts and the text fields named in `fields`.

    A body that is not multipart form data carries no file, which the route answers as the
    missing field it is. Raises `ImportTooLargeError` for a request past its bounds and
    `MalformedRequestError` for a body the parser cannot walk; every spool is closed on the
    way out of either.
    """
    content_type, params = parse_options_header(request.headers.get("content-type", ""))
    if content_type != b"multipart/form-data":
        return ImportRequest(parts=[], fields={})
    boundary = params.get(b"boundary")
    if not boundary:
        raise MalformedRequestError("Missing boundary in multipart.")

    body = _BodyReader(fields)
    charset = params.get(b"charset")
    if charset:
        body._charset = charset.decode("latin-1")
    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": body.on_part_begin,
            "on_part_data": body.on_part_data,
            "on_part_end": body.on_part_end,
            "on_header_field": body.on_header_field,
            "on_header_value": body.on_header_value,
            "on_header_end": body.on_header_end,
            "on_headers_finished": body.on_headers_finished,
        },
    )
    try:
        async for chunk in request.stream():
            parser.write(chunk)
            await body.flush()
        parser.finalize()
        await body.flush()
    except MultipartParseError as exc:
        body.close()
        raise MalformedRequestError(str(exc) or "The request body is not valid multipart form data.") from exc
    except BaseException:
        body.close()
        raise
    return ImportRequest(parts=body.finished(), fields=body.fields)
