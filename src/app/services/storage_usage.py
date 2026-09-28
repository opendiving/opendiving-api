"""What an account holds in the blob store, and the limit that bounds it.

**Used bytes are summed on read, never stored.** A counter on `user` would need row locking
to stay right under concurrent uploads, which is why `recalculate_dive_stats` recomputes
rather than counts, and these sums are over a handful of indexed per-user rows. What is
summed is what each object occupies: a dive-computer file's `stored_byte_size`, compressed;
a card scan's `byte_size`, reached through its certification's owner since the file row has
none of its own; a picture's original and rendition. Species photographs are a global
catalogue and no account's. A dive's files are hard-deleted with it, soft delete included,
so every `dive_file` row belongs to a live dive and the sum needs no join to `dive`.

**Every write for an existing account checks before its blob write**, so a refusal leaves
no orphan. Nothing locks, as nothing on the upload paths does: two writes that both pass
overshoot by at most one write, or one import's new files. Superusers are held to it like
everyone - the limit is about bytes the operator pays for.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..models.certification import Certification
from ..models.certification_file import CertificationFile
from ..models.dive_file import DiveFile
from ..models.user_picture import UserPicture

_KB = 1024
_MB = 1024 * _KB
_GB = 1024 * _MB


@dataclass(frozen=True, slots=True)
class StorageUsage:
    """One account's stored bytes, by kind."""

    dive_files_bytes: int
    certification_files_bytes: int
    pictures_bytes: int

    @property
    def used_bytes(self) -> int:
        return self.dive_files_bytes + self.certification_files_bytes + self.pictures_bytes


def storage_limit_bytes() -> int | None:
    """`STORAGE_LIMIT_MB` in bytes, or `None` when the instance sets no limit."""
    limit_mb = settings.STORAGE_LIMIT_MB
    return None if limit_mb is None else limit_mb * _MB


async def get_storage_usage(db: AsyncSession, *, user_id: int) -> StorageUsage:
    """What `user_id` holds, in one query.

    A rendition stored before `rendition_byte_size` existed reads as 0 until the lifespan
    measures it (`core/setup.py`, `measure_unsized_renditions`).
    """
    dive_files = select(func.coalesce(func.sum(DiveFile.stored_byte_size), 0)).where(DiveFile.user_id == user_id)
    certification_files = (
        select(func.coalesce(func.sum(CertificationFile.byte_size), 0))
        .join(Certification, Certification.id == CertificationFile.certification_id)
        .where(Certification.user_id == user_id)
    )
    pictures = select(
        func.coalesce(
            func.sum(
                func.coalesce(UserPicture.original_byte_size, 0) + func.coalesce(UserPicture.rendition_byte_size, 0)
            ),
            0,
        )
    ).where(UserPicture.user_id == user_id)
    row = (
        await db.execute(
            select(dive_files.scalar_subquery(), certification_files.scalar_subquery(), pictures.scalar_subquery())
        )
    ).one()
    return StorageUsage(dive_files_bytes=int(row[0]), certification_files_bytes=int(row[1]), pictures_bytes=int(row[2]))


def format_size(num_bytes: int) -> str:
    """A byte count as the web's `formatFileSize` shows it, so a refusal's figures read the
    same as the Storage section's.

    Whole KB below 1024 KB, never below 1 KB for a non-empty count; MB below 1024 MB and GB
    above, with one decimal. A tie rounds upward, as JavaScript's `Math.round` and
    `toFixed` do on these exactly representable quotients, where Python's own `round` would
    go to even - so the arithmetic is on integers rather than on `round`.
    """
    if num_bytes <= 0:
        return "0 KB"
    if num_bytes < _MB:
        return f"{max(1, (num_bytes + _KB // 2) // _KB)} KB"
    unit, name = (_MB, "MB") if num_bytes < _GB else (_GB, "GB")
    tenths = (num_bytes * 10 + unit // 2) // unit
    return f"{tenths // 10}.{tenths % 10} {name}"


def _crosses(*, used: int, limit: int, incoming: int, retired: int) -> bool:
    """The refusal rule. Only a write that grows the total can cross the limit, so one that
    shrinks or holds it passes however full the account already is."""
    return incoming > retired and used - retired + incoming > limit


async def ensure_room(
    db: AsyncSession,
    *,
    user_id: int,
    incoming: int,
    retired: int = 0,
    exact: Callable[[], Awaitable[int]] | None = None,
) -> None:
    """Refuse a write that would take `user_id` past the storage limit, with a 413.

    `incoming` is what the write adds and `retired` what it replaces, both as stored. With
    `exact`, `incoming` is a ceiling instead, and `exact` is awaited for the true figure only
    when the ceiling alone would refuse: measuring a dive-computer file means compressing it,
    which is not worth doing while even its worst case fits.

    Runs a query, so a caller that releases its read transaction before a blob write calls
    this first.
    """
    limit = storage_limit_bytes()
    if limit is None or incoming <= retired:
        return
    used = (await get_storage_usage(db, user_id=user_id)).used_bytes
    if not _crosses(used=used, limit=limit, incoming=incoming, retired=retired):
        return
    if exact is not None and not _crosses(used=used, limit=limit, incoming=await exact(), retired=retired):
        return
    raise HTTPException(
        status_code=413,
        detail=(
            f"This upload would take your account past its storage limit: {format_size(used)} of "
            f"{format_size(limit)} used."
        ),
    )
