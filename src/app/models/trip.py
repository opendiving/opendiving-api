from sqlalchemy import ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, TimestampMixin


class Trip(Base, PublicUUIDMixin, TimestampMixin):
    """A diving trip, which is a name and an ordered sequence of `trip_part` rows.

    No dates of its own: a trip's span is the earliest `start_date` and the latest
    `end_date` across its parts, so a trip that ran a liveaboard week and then a hotel
    week says so in two parts rather than flattening both into one range.
    """

    __tablename__ = "trip"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    notes: Mapped[str] = mapped_column(Text, default="")

    @declared_attr.directive
    @classmethod
    def __table_args__(cls) -> tuple:
        return (
            # Case-insensitive uniqueness per user. Reusing a deleted trip's name needs no
            # exemption here: the row is gone, so it constrains nothing.
            Index(
                "ux_trip_user_id_name_lower",
                "user_id",
                func.lower(cls.name),
                unique=True,
            ),
        )
