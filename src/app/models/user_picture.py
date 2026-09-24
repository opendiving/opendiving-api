from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, TimestampMixin

# The members an original brings with it, null together on a row that holds none.
_ORIGINAL_COLUMNS = (
    "original_storage_key",
    "original_sha256",
    "original_byte_size",
    "original_content_type",
    "original_filename",
    "crop_x",
    "crop_y",
    "crop_width",
    "crop_height",
)


class UserPicture(Base, PublicUUIDMixin, TimestampMixin):
    """One of an account's two pictures - its avatar or its check-in portrait.

    Each holds a **rendition**, the WebP every screen shows, and usually the **original** it
    was rendered from with the **crop** that framed it. The original is the upload with its
    metadata stripped losslessly (`services/picture_originals.py`); the rendition is always
    re-derived from original and crop, so an adjustment is a new crop over the same original.
    A row holds no original only for an avatar stored before originals were kept, one seeded
    from Google, or one uploaded without a crop; every portrait holds one.

    `uuid` is minted afresh with every new original: it is the Stored File uuid an export
    names, and a replaced picture is a different file.

    Bytes live in the blob store under `user-avatars/` and `user-portraits/`; the keys are
    minted per write by `blob_store.new_key`, never derived from this row, which survives
    replacement. `services/user_pictures.py` is the only module that writes one.
    """

    __tablename__ = "user_picture"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    # `PictureKind` in `schemas/user_picture.py`: "avatar" or "portrait".
    kind: Mapped[str] = mapped_column(String(16))

    rendition_storage_key: Mapped[str] = mapped_column(String(255))
    # The rendition's digest: the download route's `ETag` and the `?v=` a client appends.
    rendition_sha256: Mapped[str] = mapped_column(String(64))

    original_storage_key: Mapped[str | None] = mapped_column(String(255), default=None)
    original_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    original_byte_size: Mapped[int | None] = mapped_column(Integer, default=None)
    # Sniffed from the bytes, never the multipart header: `image/jpeg` or `image/png`.
    original_content_type: Mapped[str | None] = mapped_column(String(64), default=None)
    original_filename: Mapped[str | None] = mapped_column(String(255), default=None)
    # A rectangle in the upright original's pixels, at the picture's ratio.
    crop_x: Mapped[int | None] = mapped_column(Integer, default=None)
    crop_y: Mapped[int | None] = mapped_column(Integer, default=None)
    crop_width: Mapped[int | None] = mapped_column(Integer, default=None)
    crop_height: Mapped[int | None] = mapped_column(Integer, default=None)

    @declared_attr.directive
    @classmethod
    def __table_args__(cls) -> tuple:
        every_null = " AND ".join(f"{column} IS NULL" for column in _ORIGINAL_COLUMNS)
        none_null = " AND ".join(f"{column} IS NOT NULL" for column in _ORIGINAL_COLUMNS)
        return (
            # One of each kind per account; also the index `user_id`'s foreign key needs.
            Index("ux_user_picture_user_id_kind", "user_id", "kind", unique=True),
            # One row per stored file, as on every table holding a key: two rows naming one
            # key would let either one's replacement unlink the other's bytes.
            Index("ux_user_picture_rendition_storage_key", "rendition_storage_key", unique=True),
            Index("ux_user_picture_original_storage_key", "original_storage_key", unique=True),
            CheckConstraint(f"({every_null}) OR ({none_null})", name="ck_user_picture_original_members_together"),
            CheckConstraint(
                "crop_x IS NULL OR (crop_x >= 0 AND crop_y >= 0 AND crop_width > 0 AND crop_height > 0)",
                name="ck_user_picture_crop_positive",
            ),
        )
