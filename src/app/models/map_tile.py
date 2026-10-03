from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class MapTile(Base):
    """One square of the Web Mercator grid, `z/x/y`, drawn by the map renderer in one theme the
    first time any card or page head asked for it.

    Nothing of any record is in it - no pins, no fit - so it carries no account: one row serves
    every record and every account whose map covers that square (`services/map_tiles.py`).
    Named by the signature of the renderer that drew it as well as by its address, so a
    renderer that would draw differently names every tile afresh.

    Nobody's data: it counts toward no storage limit, is not exported, and no account's purge
    collects it. A row unserved for `UNSERVED_RETENTION` is deleted by the worker. Bytes live
    in the blob store under `map-tiles/`.
    """

    __tablename__ = "map_tile"

    id: Mapped[int] = mapped_column(autoincrement=True, nullable=False, primary_key=True, init=False)
    z: Mapped[int] = mapped_column(SmallInteger)
    x: Mapped[int] = mapped_column(Integer)
    y: Mapped[int] = mapped_column(Integer)
    # `MapTheme` in `schemas/map_tile.py`.
    theme: Mapped[str] = mapped_column(String(8))
    signature: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(255))
    # The bytes' own digest: the `ETag`.
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
    # Moved forward at most once a day by a request that finds the row, so reads stay reads.
    last_served_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))

    __table_args__ = (
        # The single-flight claim's unit.
        Index("ux_map_tile_signature_theme_z_x_y", "signature", "theme", "z", "x", "y", unique=True),
        # One row per stored file, as on every table holding a key.
        Index("ux_map_tile_storage_key", "storage_key", unique=True),
        Index("ix_map_tile_last_served_at", "last_served_at"),
        CheckConstraint("theme IN ('light', 'dark')", name="ck_map_tile_theme"),
    )
