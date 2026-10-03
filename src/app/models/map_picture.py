from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class MapPicture(Base):
    """A dive's, a trip's or a dive site's map picture, drawn by the map renderer the first time
    a card asked for it, in one theme.

    Named by `digest` - what the renderer was sent and the signature of the renderer that drew
    it (`services/map_pictures.py`) - rather than by the record, so whatever moves a record's
    places names a different picture and no write path has to know this table exists. Two
    records of one account showing the same places share a row; two accounts never do.

    Derived, so it counts toward no storage limit and is not exported. A row unserved for
    `UNSERVED_RETENTION` is deleted by the worker, and its account's purge takes the rest.
    Bytes live in the blob store under `map-pictures/`.
    """

    __tablename__ = "map_picture"

    id: Mapped[int] = mapped_column(autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    digest: Mapped[str] = mapped_column(String(64))
    # `MapTheme` in `schemas/map_picture.py`.
    theme: Mapped[str] = mapped_column(String(8))
    storage_key: Mapped[str] = mapped_column(String(255))
    # The bytes' own digest: the `ETag`.
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
    # Moved forward at most once a day by a request that finds the row, so reads stay reads.
    last_served_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))

    __table_args__ = (
        # The single-flight claim's unit, and also the index `user_id`'s foreign key needs.
        Index("ux_map_picture_user_id_digest_theme", "user_id", "digest", "theme", unique=True),
        # One row per stored file, as on every table holding a key.
        Index("ux_map_picture_storage_key", "storage_key", unique=True),
        Index("ix_map_picture_last_served_at", "last_served_at"),
        CheckConstraint("theme IN ('light', 'dark')", name="ck_map_picture_theme"),
    )
