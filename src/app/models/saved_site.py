from sqlalchemy import ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base
from ..core.db.models import TimestampMixin


class SavedSite(Base, TimestampMixin):
    """A dive site on one diver's list, with what is theirs alone about it.

    `dive_site` holds what is true of the place, and this row the diver's relation to it: their
    notes, and in `created_at` when the site entered their logbook, which is what a site read
    answers. Every site has one for its owner, written with the site, so a read of a diver's
    sites starts here and a diver's private text sits nowhere on the row that describes the place.
    """

    __tablename__ = "saved_site"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    # No standalone index: the unique constraint below leads with it.
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    dive_site_id: Mapped[int] = mapped_column(ForeignKey("dive_site.id", ondelete="CASCADE"), index=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (UniqueConstraint("user_id", "dive_site_id", name="ux_saved_site_user_id_dive_site_id"),)
