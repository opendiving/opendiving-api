from sqlalchemy import ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class DiveTag(Base):
    """Join table listing a dive's tags, in the diver's order.

    `DivePerson`'s shape without a role. A tag's delete takes its rows; a dive is
    soft-deleted and keeps them, which is why a tag's count is of live dives.
    """

    __tablename__ = "dive_tag"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    # No standalone index: both constraints below lead with it.
    dive_id: Mapped[int] = mapped_column(ForeignKey("dive.id", ondelete="CASCADE"))
    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("dive_id", "tag_id", name="ux_dive_tag_dive_id_tag_id"),
        Index("ix_dive_tag_dive_id_position", "dive_id", "position"),
    )
