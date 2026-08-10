from sqlalchemy import ForeignKey, Index, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, declared_attr, deferred, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, TimestampMixin


class CertificationFile(Base, PublicUUIDMixin, TimestampMixin):
    """One stored image or PDF of a certification card - its front or its back.

    The bytes live in Postgres (`data`, a `bytea`) rather than in object storage. At this
    scale that is the right trade: a diver has well under ten cards of a few hundred KB
    to a few MB each, so the existing database and its backups cover the lot with no new
    infrastructure, no credentials to manage and no orphaned-object cleanup. Every read
    and write goes through `services/certification_files.py`, which is the seam to
    replace if a future feature (dive photo galleries) makes object storage worth it.

    A separate table rather than two blob columns on `certification`: even `deferred()`
    columns are easy to load by accident from a `get_multi` or an admin view, and putting
    them out of reach makes that structurally impossible. It also means front and back
    get identical handling instead of duplicated column pairs.

    No `SoftDeleteMixin`. Soft-deleting a blob leaves it occupying its bytes in the table
    forever with nothing able to read it; these are hard-deleted, including when their
    parent certification is soft-deleted (see `erase_certification`).
    """

    __tablename__ = "certification_file"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, unique=True, primary_key=True, init=False)
    certification_id: Mapped[int] = mapped_column(ForeignKey("certification.id", ondelete="CASCADE"), index=True)
    # "front" | "back" - see `CertificationSide` in `schemas/certification.py`. Cards are
    # two-sided and both matter: the front carries the diver's name and level, the back
    # the certification number and issue date.
    side: Mapped[str] = mapped_column(String(8))
    # The content type as **sniffed from the file's leading bytes**, never the client's
    # claimed `Content-Type` - it is what `read_certification_file` later serves the
    # bytes back as, so trusting the uploader here would let someone have us serve
    # arbitrary content under a type of their choosing.
    content_type: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(Integer)
    original_filename: Mapped[str] = mapped_column(String(255))
    # Hex SHA-256 of `data`. Serves as the `ETag` on the download endpoint (so a card the
    # diver opens repeatedly re-validates with a 304 instead of re-sending megabytes) and
    # as an integrity check against the stored bytes.
    sha256: Mapped[str] = mapped_column(String(64))
    # `deferred` so that any query for a file row - including one written later by
    # someone who has not read this file - returns metadata only unless the bytes are
    # asked for explicitly with `undefer`.
    #
    # `nullable=False` is spelled out because wrapping the column in `deferred()` hides
    # the `Mapped[bytes]` annotation from SQLAlchemy's nullability inference, which would
    # otherwise emit a nullable column - and a file row with no bytes is meaningless.
    data: Mapped[bytes] = deferred(mapped_column(LargeBinary, nullable=False))

    @declared_attr.directive
    @classmethod
    def __table_args__(cls) -> tuple:
        return (
            # One file per side. Re-uploading a side replaces it rather than accumulating
            # versions: a diver retaking a blurry photo of their card wants to fix it, not
            # to keep the blur. Enforced here as well as in `store_certification_file` so
            # two concurrent uploads of the same side can't both win.
            Index(
                "ux_certification_file_certification_id_side",
                "certification_id",
                "side",
                unique=True,
            ),
        )
