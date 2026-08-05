from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, SoftDeleteMixin, TimestampMixin


class Dive(Base, PublicUUIDMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "dive"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, unique=True, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    dive_number: Mapped[int] = mapped_column(Integer)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration: Mapped[int] = mapped_column(Integer)
    notes: Mapped[str] = mapped_column(Text)

    max_depth: Mapped[float | None] = mapped_column(Float, default=None)
    avg_depth: Mapped[float | None] = mapped_column(Float, default=None)
    bottom_temperature: Mapped[float | None] = mapped_column(Float, default=None)
    visibility: Mapped[int | None] = mapped_column(Integer, default=None)
    trip_id: Mapped[int | None] = mapped_column(ForeignKey("trip.id", ondelete="SET NULL"), default=None, index=True)

    @declared_attr.directive
    @classmethod
    def __table_args__(cls) -> tuple:
        return (
            # Mirrors the frontend's Zod validation (`lib/validations/dive.ts`) at the DB
            # layer, so direct API calls or bugs can't insert nonsensical dive data.
            CheckConstraint("duration > 0", name="ck_dive_duration_positive"),
            CheckConstraint("visibility IS NULL OR visibility >= 0", name="ck_dive_visibility_non_negative"),
            CheckConstraint("max_depth IS NULL OR max_depth > 0", name="ck_dive_max_depth_positive"),
            CheckConstraint("avg_depth IS NULL OR avg_depth > 0", name="ck_dive_avg_depth_positive"),
            # Serves `_cached_read_dives` (`GET /dives`, by far the hottest query on this
            # table): `WHERE user_id = ... AND is_deleted = false ORDER BY start_time DESC`.
            # `is_deleted` isn't a column here - the partial predicate already pins it to
            # `false`, so Postgres can use this index for both the filter and the sort
            # without a separate sort step, while staying smaller than a 3-column index.
            # This replaces the old standalone `is_deleted` index, which was low-value as a
            # leading column (mostly `false`) and unused elsewhere on this table (every
            # other dive lookup filters by the `id` primary key instead).
            Index(
                "ix_dive_user_id_start_time",
                "user_id",
                cls.start_time.desc(),
                postgresql_where=cls.is_deleted.is_(False),
            ),
            # Serves `services.dive_stats.recalculate_dive_stats`'s
            # `COUNT`/`MAX(max_depth)`/`SUM(duration)` aggregate, run after every dive
            # create/update/delete. `max_depth`/`duration` are `INCLUDE`d (not index key
            # columns) purely so Postgres can answer the aggregate as an index-only scan
            # instead of a heap fetch per matching dive - they aren't used for filtering or
            # ordering, so they don't need to be part of the index's sort key.
            Index(
                "ix_dive_user_id_stats",
                "user_id",
                postgresql_where=cls.is_deleted.is_(False),
                postgresql_include=["max_depth", "duration"],
            ),
        )
