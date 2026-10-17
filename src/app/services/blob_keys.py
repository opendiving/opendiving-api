"""Every column that stores a blob-store key: the one list the sweeper, the purge and the
startup check read.

`blob_store` knows where the bytes live; this module knows which rows point at them. The
orphan sweeper (`src/scripts/sweep_orphaned_files.py`) treats any stored key absent from
these columns as an orphan, so **a blob kind missing here is deleted by `--delete`**.
`tests/test_blob_keys.py` fails when a model gains a key column this list lacks.

`owner` is how the account purge finds one diver's keys, or `None` for a kind that belongs
to no account - a species photo or a map tile is shared by everyone, and taking it with one
account would take it from all of them.
"""

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import InstrumentedAttribute

from ..models.certification import Certification
from ..models.certification_file import CertificationFile
from ..models.dive_file import DiveFile
from ..models.map_tile import MapTile
from ..models.species import Species
from ..models.user_picture import UserPicture


@dataclass(frozen=True, slots=True)
class BlobKeyColumn:
    column: InstrumentedAttribute[str | None]
    owner: Callable[[int], ColumnElement[bool]] | None


BLOB_KEY_COLUMNS: tuple[BlobKeyColumn, ...] = (
    BlobKeyColumn(DiveFile.storage_key, lambda user_id: DiveFile.user_id == user_id),
    # `certification_file` has no `user_id` of its own; it hangs off `certification`.
    BlobKeyColumn(
        CertificationFile.storage_key,
        lambda user_id: CertificationFile.certification_id.in_(
            select(Certification.id).where(Certification.user_id == user_id)
        ),
    ),
    BlobKeyColumn(UserPicture.rendition_storage_key, lambda user_id: UserPicture.user_id == user_id),
    BlobKeyColumn(UserPicture.original_storage_key, lambda user_id: UserPicture.user_id == user_id),
    BlobKeyColumn(Species.photo_storage_key, None),
    BlobKeyColumn(MapTile.storage_key, None),
)
