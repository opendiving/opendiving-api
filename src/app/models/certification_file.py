from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, TimestampMixin


class CertificationFile(Base, PublicUUIDMixin, TimestampMixin):
    """One stored image or PDF of a certification card - its front or its back.

    The bytes live on the files volume, not in this table: the row carries a
    `storage_key` and `services/blob_store.py` holds the file. Every read and write still
    goes through `services/certification_files.py`, which is the module that knows a card
    has a file at all - see *"File payloads live on the files volume, not in Postgres"* in
    `DECISIONS.md` for why they left Postgres, and the superseded section it names for the
    trade that held before photo-scale storage was on the roadmap.

    A separate table rather than two columns on `certification`: it keeps front and back
    identically handled instead of duplicating column pairs, and keeps a `get_multi` or an
    admin view over certifications from touching file rows at all. That reasoning predates
    the move (it was about `deferred()` blobs being easy to load by accident) and survives
    it.

    No `SoftDeleteMixin`. A soft-deleted row holds a file nothing can read; these are
    hard-deleted, including when their parent certification is soft-deleted (see
    `erase_certification`). The stored file goes with the row, unlinked after the deleting
    transaction commits.
    """

    __tablename__ = "certification_file"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
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
    # Hex SHA-256 of the stored bytes. Serves as the `ETag` on the download endpoint (so a
    # card the diver opens repeatedly re-validates with a 304 instead of re-sending
    # megabytes), as an integrity check against the stored file, and as half of
    # `storage_key`.
    sha256: Mapped[str] = mapped_column(String(64))
    # Where the bytes are, in whichever store `blob_store` is configured for:
    # `certification-files/{sha256[:2]}/{nonce}_{sha256}`, minted by `blob_store.new_key`.
    # Opaque to everything but that module, and deliberately spelled so that it is a valid
    # S3 object key and a relative path at once - which is what lets an instance move
    # between the two backends without rewriting a single row.
    #
    # **The nonce is per write, deliberately not this row's uuid.** This row survives
    # replacement (the upsert preserves its uuid), so a key derived from it would be keyed
    # on (slot, content) and could be re-minted after being retired - see
    # `blob_store.new_key`. Every replacement therefore mints a new key, which also keeps
    # stored files immutable: a reader mid-replacement can never be handed new bytes under
    # the old metadata.
    storage_key: Mapped[str] = mapped_column(String(255))

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
            # One row per stored file, for the same reason as `dive_file`'s: two rows
            # naming one key would let either one's deletion unlink the other's bytes.
            Index("ux_certification_file_storage_key", "storage_key", unique=True),
        )
