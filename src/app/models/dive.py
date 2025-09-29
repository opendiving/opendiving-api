import uuid as uuid_pkg
from datetime import UTC, datetime
from uuid6 import uuid7

from sqlalchemy import DateTime, ForeignKey, String, Integer, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class Dive(Base):
    __tablename__ = "dive"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, unique=True, primary_key=True, init=False)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    dive_number: Mapped[int] = mapped_column(Integer)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str] = mapped_column(String(63206))
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), default_factory=uuid7, unique=True)

    max_depth: Mapped[int] = mapped_column(Integer, default=None)
    avg_depth: Mapped[int] = mapped_column(Integer, default=None)
    bottom_temp: Mapped[int] = mapped_column(Integer, default=None)
    location_lat: Mapped[int] = mapped_column(Integer, default=None)
    location_long: Mapped[int] = mapped_column(Integer, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    is_deleted: Mapped[bool] = mapped_column(default=False, index=True)
