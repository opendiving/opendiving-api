from sqlalchemy import ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class DiveGearItem(Base):
    """Join table linking a dive to the gear items used on it.

    `position` preserves the order the items were listed in on the dive form, so a
    dive's gear reads back the way the diver entered it (typically the order of the
    gear set it was loaded from). Mirrors `DiveDiveSite` - see `crud_dive_gear_items.py`,
    which replaces a dive's whole gear list wholesale rather than diffing it.
    """

    __tablename__ = "dive_gear_item"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    # No standalone index on dive_id: the composite index below (leading column dive_id)
    # already serves lookups filtered by dive_id alone, plus satisfies the ORDER BY position
    # used by get_gear_items_for_dive/get_gear_items_for_dives without an extra index.
    dive_id: Mapped[int] = mapped_column(ForeignKey("dive.id", ondelete="CASCADE"))
    # Indexed for `recalculate_gear_dive_counts`'s per-item COUNT and for filtering
    # `GET /dives?gear_item_uuid=...`; not a leading column of any other index here.
    gear_item_id: Mapped[int] = mapped_column(ForeignKey("gear_item.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("dive_id", "gear_item_id", name="ux_dive_gear_item_dive_id_gear_item_id"),
        Index("ix_dive_gear_item_dive_id_position", "dive_id", "position"),
    )
