from sqlalchemy import ForeignKey, Integer, UniqueConstraint
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
    dive_id: Mapped[int] = mapped_column(ForeignKey("dive.id", ondelete="CASCADE"), index=True)
    dive_site_id: Mapped[int] = mapped_column(ForeignKey("dive_site.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (UniqueConstraint("dive_id", "dive_site_id", name="ux_dive_dive_site_dive_id_dive_site_id"),)
