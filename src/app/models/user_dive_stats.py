from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class UserDiveStats(Base):
    """Aggregate dive statistics for a user.

    One row per user, kept in sync by `services.dive_stats.recalculate_dive_stats`
    whenever a dive is created, updated, or deleted (soft or hard) for that user.
    """

    __tablename__ = "user_dive_stats"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, unique=True, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), unique=True, index=True)

    total_dives: Mapped[int] = mapped_column(Integer, default=0)
    max_depth: Mapped[float] = mapped_column(Float, default=0)
    total_time: Mapped[int] = mapped_column(Integer, default=0, doc="Total dive time in seconds")
    species_seen: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
