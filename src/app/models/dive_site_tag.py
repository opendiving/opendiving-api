from sqlalchemy import ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class DiveSiteTag(Base):
    """Join table listing a dive site's tags, in the diver's order.

    `DiveTag`'s shape for the other host: the diver keeps one vocabulary, so a tag renames
    across dives and sites alike and its delete takes its rows from both.
    """

    __tablename__ = "dive_site_tag"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    # No standalone index: both constraints below lead with it.
    dive_site_id: Mapped[int] = mapped_column(ForeignKey("dive_site.id", ondelete="CASCADE"))
    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("dive_site_id", "tag_id", name="ux_dive_site_tag_dive_site_id_tag_id"),
        Index("ix_dive_site_tag_dive_site_id_position", "dive_site_id", "position"),
    )
