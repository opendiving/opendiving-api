"""The files an import stores, named in rows before a byte of them is written.

An import of several files cannot know what it stores until it has planned and written the
last of them: each file is planned against the logbook the files before it left, and a
second export of one recording stores nothing where its bytes are already there. Yet the
storage limit refuses the whole import before anything is written, at the preview and again
at the apply. So the writer mints each file's key and writes its row inside the import's
transaction, and holds the bytes back here; the batch then checks the account's room over
everything held, and only an apply that fits writes the objects - all of them before the
commit, which is the ordering every other write keeps: every database-visible state names
bytes that exist. A preview never writes one, and rolls its rows back.

What a row records of its object's size before the object exists is the most it can occupy;
the write corrects it to what the store reports.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from ...models.dive_file import DiveFile
from .. import blob_store
from ..dive_files import FileExtraction


@dataclass(frozen=True, slots=True)
class _Held:
    kind: str
    size: int
    read: Callable[[], bytes]


def _kind_of(key: str) -> str:
    """The kind a key was minted under - its first segment (`blob_store.new_key`)."""
    return key.split("/", 1)[0]


@dataclass(slots=True)
class StagedFiles:
    """The objects one import has named in rows and not yet written.

    `extractions` are the imported files' own, by digest, so a recording given a second file
    of the import re-derives from its files without reading the first one again.
    """

    _held: dict[str, _Held] = field(default_factory=dict)
    extractions: dict[str, FileExtraction] = field(default_factory=dict)

    def stage(self, *, kind: str, sha256: str, size: int, read: Callable[[], bytes]) -> tuple[str, int]:
        """Mint a key for one object and hold its bytes back.

        Returns the key and the size its row records until the object is written: exact for
        a kind stored as it is, the most a compressed one can occupy otherwise.
        """
        key = blob_store.new_key(kind, sha256=sha256)
        self._held[key] = _Held(kind=kind, size=size, read=read)
        return key, blob_store.stored_size_ceiling(kind, size)

    async def put(self, key: str, data: bytes) -> int:
        """`blob_store.put`'s signature, holding `data` back under a key the caller minted."""
        kind = _kind_of(key)
        self._held[key] = _Held(kind=kind, size=len(data), read=lambda: data)
        return blob_store.stored_size_ceiling(kind, len(data))

    @property
    def held(self) -> Mapping[str, Callable[[], bytes]]:
        """Each held object's bytes, by key, for a re-read before the objects are written."""
        return {key: held.read for key, held in self._held.items()}

    def ceiling(self) -> int:
        """The most the held objects can occupy once written."""
        return sum(blob_store.stored_size_ceiling(held.kind, held.size) for held in self._held.values())

    async def exact(self) -> int:
        """What the held objects occupy once written, measured the way `blob_store.put` stores
        them - a compression per dive-computer file, so asked only where the ceiling does not
        fit."""
        return sum([await blob_store.stored_size(held.kind, held.read()) for held in self._held.values()])

    async def write(self, db: AsyncSession) -> None:
        """Write every held object, before the caller commits the rows that name them.

        A dive-computer file's row records its object's size, which only the write knows, so
        the row is corrected to it. No compensating unlink on a failure: an object written
        and then orphaned by a rollback is the recorded and accepted orphan case, reclaimed by
        `sweep_orphaned_files.py`.
        """
        for key, held in self._held.items():
            stored = await blob_store.put(key, held.read())
            if blob_store.compresses(held.kind):
                await db.execute(update(DiveFile).where(DiveFile.storage_key == key).values(stored_byte_size=stored))
        self._held.clear()
