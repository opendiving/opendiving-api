from sqlalchemy import ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class GearSetItem(Base):
    """Join table linking a gear set to the gear items it contains.

    `position` preserves the order the items were added in, which is the order they
    get loaded into the dive form when the set is selected. Both FKs are
    `ON DELETE CASCADE`: hard-deleting a set drops its membership rows (not the items),
    and hard-deleting an item removes it from every set without affecting the rest.
    """

    __tablename__ = "gear_set_item"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, unique=True, primary_key=True, init=False)
    # No standalone index on gear_set_id - the composite index below covers it, see
    # the same note on `DiveGearItem`.
    gear_set_id: Mapped[int] = mapped_column(ForeignKey("gear_set.id", ondelete="CASCADE"))
    gear_item_id: Mapped[int] = mapped_column(ForeignKey("gear_item.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("gear_set_id", "gear_item_id", name="ux_gear_set_item_gear_set_id_gear_item_id"),
        Index("ix_gear_set_item_gear_set_id_position", "gear_set_id", "position"),
    )
