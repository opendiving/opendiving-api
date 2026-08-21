from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, TimestampMixin


class GearItem(Base, PublicUUIDMixin, TimestampMixin):
    """A single piece of diving equipment owned (or rented) by a user, e.g. a
    regulator, BCD, drysuit or dive computer.

    Gear items live independently of any dive: a dive references the items used on
    it via the `dive_gear_item` join table, and `GearSet`s group items together for
    convenience when logging (see `models/gear_set.py`).
    """

    __tablename__ = "gear_item"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    brand: Mapped[str | None] = mapped_column(String(255), default=None)
    # Broad category ("fins", "wetsuit", "regulator", ...) - see `GearType` in
    # `schemas/gear_item.py` for the vocabulary, which is the single source of truth.
    # Stored as a plain string rather than a Postgres ENUM or a `CHECK` constraint: the
    # API's own Pydantic schema already rejects unknown values on every write (unlike
    # the numeric ranges on `dive`/`dive_mixture`, whose only other validation lives in
    # the frontend's Zod schemas - see DECISIONS.md), so a DB-level copy of the list
    # would buy nothing and would need a DDL change every time a category is added.
    # 32 chars is generous headroom over the longest current member ("regulator"),
    # so a new category never needs the column widened.
    type: Mapped[str | None] = mapped_column(String(32), default=None)
    notes: Mapped[str] = mapped_column(Text, default="")
    # Rented gear is a property of the item itself rather than of a dive: a diver
    # typically adds "rented BCD (Blue Ocean, Koh Tao)" as its own item and archives
    # it once the trip is over, rather than re-flagging their own gear per dive.
    rented: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # Archiving hides retired/sold/returned gear from the dive form's picker without
    # deleting it, so historical dives keep referencing it and its `dive_count` stays
    # meaningful. It is the non-destructive path, and the only one: a `DELETE` takes the
    # item, its schedules, its service records and every join row with it.
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # Number of the owner's non-deleted dives this item was used on. Denormalized and
    # kept in sync by `services.gear_stats.recalculate_gear_dive_counts` after every
    # dive create/update/delete, mirroring how `user_dive_stats` is maintained - the
    # gear list view shows it for every row, so computing it per request would mean a
    # join + GROUP BY on every page load of a cache miss.
    dive_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    @declared_attr.directive
    @classmethod
    def __table_args__(cls) -> tuple:
        return (
            # Case-insensitive uniqueness per user on (brand, name). COALESCE maps a NULL
            # brand to '' so two brand-less items with the same name are also considered
            # duplicates. Two genuinely identical items (e.g. a pair of matching stage
            # cylinders) are expected to be told apart by name ("Stage 1"/"Stage 2"), which
            # is also what makes their per-item dive counts meaningful. Archived items still
            # hold their slot - archiving is not deleting.
            Index(
                "ux_gear_item_user_id_brand_name_lower",
                "user_id",
                func.coalesce(func.lower(cls.brand), ""),
                func.lower(cls.name),
                unique=True,
            ),
            # Serves `read_gear_items` (`GET /gear-items`): `WHERE user_id = ...
            # [AND is_archived = false] ORDER BY name ASC`. Keyed on the plain
            # (case-sensitive) `name` so it can satisfy the ORDER BY, which the
            # `lower(name)`-keyed unique index above can't.
            Index(
                "ix_gear_item_user_id_name",
                "user_id",
                "name",
            ),
        )
