from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class DiveSite(Base):
    __tablename__ = "dive_site"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, unique=True, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    notes: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str | None] = mapped_column(String(255), default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    is_deleted: Mapped[bool] = mapped_column(default=False)

    __table_args__ = (
        # Case-insensitive uniqueness per user on (name, location), ignoring soft-deleted
        # dive sites so the combination can be reused once a site has been "deleted".
        # COALESCE maps NULL location to '' so two NULL-location sites with the same name
        # are also considered duplicates.
        Index(
            "ux_dive_site_user_id_name_location_lower",
            "user_id",
            func.lower(name),
            func.coalesce(func.lower(location), ""),
            unique=True,
            postgresql_where=is_deleted.is_(False),
        ),
        # Serves `read_dive_sites` (`GET /dive-sites`): `WHERE user_id = ... AND
        # is_deleted = false ORDER BY name ASC`. Distinct from the unique index above,
        # which is keyed on `lower(name)` and can't satisfy a plain (case-sensitive)
        # `ORDER BY name`. Replaces the old standalone `is_deleted` index, which was
        # low-value as a leading column and unused elsewhere on this table (every other
        # dive site lookup filters by the `id` primary key instead).
        Index(
            "ix_dive_site_user_id_name",
            "user_id",
            "name",
            postgresql_where=is_deleted.is_(False),
        ),
    )
