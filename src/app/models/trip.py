from datetime import date

from sqlalchemy import Date, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, TimestampMixin


class Trip(Base, PublicUUIDMixin, TimestampMixin):
    __tablename__ = "trip"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, unique=True, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    start_date: Mapped[date] = mapped_column(Date)
    notes: Mapped[str] = mapped_column(Text, default="")
    end_date: Mapped[date | None] = mapped_column(Date, default=None)

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
            # Serves `read_trips` (`GET /trips`): `WHERE user_id = ... ORDER BY start_date
            # DESC`. Replaces the old standalone `is_deleted` index, which was low-value as
            # a leading column and unused elsewhere on this table (every other trip lookup
            # filters by the `id` primary key instead).
            Index(
                "ix_trip_user_id_start_date",
                "user_id",
                cls.start_date.desc(),
            ),
        )
