from sqlalchemy import ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class DiveDiveSite(Base):
    """Join table linking a dive to the one or more dive sites visited during it.

    A dive usually has a single site, but drift dives (or any dive that crosses
    more than one named site) can reference several. `position` preserves the
    order the sites were visited in - 0 is the first/primary site, shown as the
    dive's main site wherever only one can be displayed.
    """

    __tablename__ = "dive_dive_site"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, unique=True, primary_key=True, init=False)
    # No standalone index on dive_id: the composite index below (leading column dive_id)
    # already serves lookups filtered by dive_id alone, plus satisfies the ORDER BY position
    # used by get_dive_sites_for_dive/get_dive_sites_for_dives without an extra index.
    dive_id: Mapped[int] = mapped_column(ForeignKey("dive.id", ondelete="CASCADE"))
    dive_site_id: Mapped[int] = mapped_column(ForeignKey("dive_site.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("dive_id", "dive_site_id", name="ux_dive_dive_site_dive_id_dive_site_id"),
        Index("ix_dive_dive_site_dive_id_position", "dive_id", "position"),
    )
