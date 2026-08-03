from datetime import UTC, date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class Trip(Base):
    __tablename__ = "trip"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, unique=True, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    start_date: Mapped[date] = mapped_column(Date)

    location: Mapped[str | None] = mapped_column(String(255), default=None)
    end_date: Mapped[date | None] = mapped_column(Date, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    is_deleted: Mapped[bool] = mapped_column(default=False, index=True)

    __table_args__ = (
        # Case-insensitive uniqueness per user, ignoring soft-deleted trips so a
        # name can be reused once its previous trip has been "deleted".
        Index(
            "ux_trip_user_id_name_lower",
            "user_id",
            func.lower(name),
            unique=True,
            postgresql_where=is_deleted.is_(False),
        ),
    )
