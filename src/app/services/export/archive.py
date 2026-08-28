"""Builds the full-export zip: every generated document plus every stored binary.

Layout, and what each member is for:

```
export.json          the complete structured export - see schemas/export.py
dives.uddf           the same bytes GET /export/uddf serves
csv/dives.csv        the flat spreadsheet view, plus eight normalized files beside it
avatar.webp          the diver's profile picture, if they have one
files/...            every stored dive-computer export, under a per-dive name
certifications/...   both sides of every stored c-card
```

**Memory.** The archive is written into a `SpooledTemporaryFile`, which keeps small
exports entirely in RAM and spills to disk past `SPOOL_THRESHOLD` - so a diver with three
dives never touches the filesystem and one with eight hundred never holds their whole
logbook in memory. The alternative, a chunked-zip dependency, would let the response
stream without a temp file at all but costs a runtime dependency and a `Content-Length`
(browsers show no progress bar without one). Revisit if profiling ever says the temp file
hurts; do not start there.

**The CSV members are written on the event loop.** `_write_text_stream` drains a whole
synchronous generator with no await in it, so each of the nine is an uninterrupted
stretch of CPU - about 7 ms for the largest (`dives.csv`) over a 500-dive corpus, and
proportional from there. Unlike `/export/csv`, which hands its drain to a thread, this one
sits inside the open `ZipFile` and cannot simply be moved off; the two document writers
above do interleave, because each awaits `load_profile` per dive. Small enough to leave,
large enough to name.

**The profiles are read twice.** `export.json` and `dives.uddf` both embed every dive's
samples, and each writer does its own per-dive `load_profile` with `undefer(data)` - so a
thousand-dive log issues two thousand of the export's most expensive query. Loading them
once and holding them would defeat the whole memory argument above, and interleaving the
two members is not possible (`ZipFile` allows one open member at a time). So it is
accepted rather than solved, and named here so nobody rediscovers it as a mystery.

The blobs are the reason the bound matters, and they are read **one file at a time** -
they live on the files volume rather than in the database now, and the loop below never
holds more than the file it is currently writing. `ZIP_STORED`, not `ZIP_DEFLATED`, for
those two directories: the stored exports are already-compressed FIT binaries and the
c-cards are JPEG/PNG/PDF, so deflating them burns CPU proportional to the whole archive
to save nothing. The generated documents *do* deflate, and XML and CSV compress about
ten to one.

**Certification images are personal documents.** The archive is served only to their
owner, over a bearer token, and never cached (see `api/v1/export.py`) - but "export" now
means a zip that includes ID-like scans, which is worth stating rather than discovering.
"""

import logging
import tempfile
import zipfile
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from typing import IO

from sqlalchemy.ext.asyncio import AsyncSession

from ...schemas.certification import CertificationSide
from ..blob_store import BlobMissingError
from ..certification_files import load_certification_file
from ..dive_files import load_dive_file
from ..user_avatars import AVATAR_FILENAME, StoredAvatar, read_avatar_bytes
from .envelope import write_export_json
from .loader import ExportBundle
from .paths import ArchivePaths, plan_archive_paths
from .tabular import CSV_WRITERS
from .uddf import write_uddf

logger = logging.getLogger(__name__)

# Where `SpooledTemporaryFile` stops holding the archive in memory and rolls it onto
# disk. 32 MB covers a few hundred dives with their exports and c-cards, which is most
# real logbooks, without letting a large one become a resident-memory problem.
SPOOL_THRESHOLD = 32 * 1024 * 1024

EXPORT_JSON_NAME = "export.json"
UDDF_NAME = "dives.uddf"
CSV_DIRECTORY = "csv"


def new_spool() -> IO[bytes]:
    """A temp file that starts in memory and spills to disk. Deleted when closed."""
    return tempfile.SpooledTemporaryFile(max_size=SPOOL_THRESHOLD)


async def spool(chunks: AsyncIterator[bytes]) -> IO[bytes]:
    """Drain an async byte stream into a spooled temp file, rewound and ready to read.

    Every export endpoint goes through here rather than handing its generator straight to
    a `StreamingResponse`, because that generator is consumed *after* the endpoint returns
    - by which point FastAPI has closed the request's database session and every lazy read
    inside it would fail. Draining while the session is alive also means the response
    carries a real `Content-Length`.
    """
    buffer = new_spool()
    try:
        async for chunk in chunks:
            buffer.write(chunk)
    except BaseException:
        buffer.close()
        raise
    buffer.seek(0)
    return buffer


def spool_text(chunks: Iterator[str]) -> IO[bytes]:
    """`spool`, for the synchronous text generators in `tabular.py`."""
    buffer = new_spool()
    try:
        for chunk in chunks:
            buffer.write(chunk.encode("utf-8"))
    except BaseException:
        buffer.close()
        raise
    buffer.seek(0)
    return buffer


def _member(name: str, exported_at: datetime, *, compress_type: int) -> zipfile.ZipInfo:
    """A member header stamped with the export's own timestamp.

    Built explicitly rather than letting `ZipFile.writestr`/`open` default it, because
    those reach for `datetime.now()` - which would make two exports of an unchanged
    logbook differ byte for byte and put a golden-file test out of reach.
    """
    info = zipfile.ZipInfo(name, date_time=exported_at.timetuple()[:6])
    info.compress_type = compress_type
    # Regular file (`0o100000`), rw-r--r--. `ZipInfo(...)` leaves this at 0, which some
    # extractors read as "no permissions" and restore as an unreadable file. The file-type
    # bits matter as much as the mode: this is the whole `st_mode`, the way
    # `ZipInfo.from_file` builds it, so an extractor reconstructing one gets a regular
    # file rather than a type of 0.
    info.external_attr = 0o100644 << 16
    return info


async def _write_stream(archive: zipfile.ZipFile, info: zipfile.ZipInfo, chunks: AsyncIterator[bytes]) -> None:
    """Write a generated document into the archive without buffering it twice.

    `ZipFile.open(..., "w")` gives a writable member handle, so the generator's chunks go
    straight through the compressor into the archive's own spool file.
    """
    # `force_zip64` because the size is not known when the header is written: `zipfile`
    # would emit a non-ZIP64 local header and then raise at member close if the generator
    # produced more than 2 GiB - after writing all of it. `export.json` embeds every
    # dive's samples, so that ceiling is reachable by a large enough logbook. The blob
    # members go through `writestr`, which knows its length and sizes the header itself.
    with archive.open(info, "w", force_zip64=True) as member:
        async for chunk in chunks:
            member.write(chunk)


def _write_text_stream(archive: zipfile.ZipFile, info: zipfile.ZipInfo, chunks: Iterator[str]) -> None:
    with archive.open(info, "w", force_zip64=True) as member:
        for chunk in chunks:
            member.write(chunk.encode("utf-8"))


async def write_archive(db: AsyncSession, bundle: ExportBundle, *, exported_at: datetime) -> IO[bytes]:
    """Build the whole archive and return it rewound, ready to stream.

    The caller owns the returned file and must close it - which is what deletes it.
    """
    paths = plan_archive_paths(bundle)
    buffer = new_spool()
    try:
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            await _write_stream(
                archive,
                _member(EXPORT_JSON_NAME, exported_at, compress_type=zipfile.ZIP_DEFLATED),
                write_export_json(db, bundle, exported_at=exported_at, paths=paths),
            )
            await _write_stream(
                archive,
                _member(UDDF_NAME, exported_at, compress_type=zipfile.ZIP_DEFLATED),
                write_uddf(db, bundle, exported_at=exported_at),
            )
            for filename, writer in CSV_WRITERS:
                _write_text_stream(
                    archive,
                    _member(f"{CSV_DIRECTORY}/{filename}", exported_at, compress_type=zipfile.ZIP_DEFLATED),
                    writer(bundle),
                )
            await _write_blobs(db, archive, bundle, paths, exported_at)
    except BaseException:
        buffer.close()
        raise

    buffer.seek(0)
    return buffer


async def _write_blobs(
    db: AsyncSession,
    archive: zipfile.ZipFile,
    bundle: ExportBundle,
    paths: ArchivePaths,
    exported_at: datetime,
) -> None:
    """Every stored binary, one row at a time.

    A file that has vanished between the metadata read and this loop is skipped rather
    than failing the export: losing a member beats losing the archive. `export.json` still
    names it, which is the honest record of what was there when the export began.

    Two ways to vanish now, and both are skipped on the same terms. A missing *row* is a
    concurrent delete from another session. A missing *file* - `BlobMissingError` - is data
    loss or a half-mounted files volume, which is logged at error level because it is an
    operational problem rather than a race; the export is precisely the tool someone
    reaches for when their volume is half-dead, so failing the whole archive over it would
    take away the one thing still working.

    The avatar comes off the `user` row already in the bundle rather than out of a query
    of its own, and it is `ZIP_STORED` like the rest: it is a WebP, and deflating an
    already-compressed image burns CPU to save nothing.
    """
    if bundle.user.avatar_storage_key and bundle.user.avatar_sha256:
        avatar_ref = StoredAvatar(storage_key=bundle.user.avatar_storage_key, sha256=bundle.user.avatar_sha256)
        try:
            avatar = await read_avatar_bytes(avatar_ref)
        except BlobMissingError:
            logger.error("Skipping the profile picture: its stored file is missing from the volume")
        else:
            archive.writestr(_member(AVATAR_FILENAME, exported_at, compress_type=zipfile.ZIP_STORED), avatar)

    for dive in bundle.dives:
        member = paths.dive_files.get(dive.id)
        if member is None:
            continue
        try:
            stored = await load_dive_file(db, dive_id=dive.id)
        except BlobMissingError:
            logger.error("Skipping dive %s's export: its stored file is missing from the volume", dive.id)
            continue
        if stored is None:
            continue
        archive.writestr(_member(member, exported_at, compress_type=zipfile.ZIP_STORED), stored.data)

    for certification in bundle.certifications:
        for info in bundle.cert_files_by_cert.get(certification.id, []):
            member = paths.certification_files.get((certification.id, info.side.value))
            if member is None:
                continue
            try:
                card = await load_certification_file(
                    db, certification_id=certification.id, side=CertificationSide(info.side)
                )
            except BlobMissingError:
                logger.error(
                    "Skipping certification %s's %s card image: its stored file is missing from the volume",
                    certification.id,
                    info.side.value,
                )
                continue
            if card is None:
                continue
            archive.writestr(_member(member, exported_at, compress_type=zipfile.ZIP_STORED), card.data)
