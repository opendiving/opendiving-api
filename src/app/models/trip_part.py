from datetime import date

from sqlalchemy import Date, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class TripPart(Base):
    """One stretch of a trip: an optional date range and an optional place.

    A trip stores no dates of its own - its span is the earliest `start_date` and the
    latest `end_date` across its parts, and a trip whose parts carry none has no span at
    all. That is what lets one trip be a liveaboard week followed by a hotel week, which a
    single span and a list of placeless names could not say.

    Every field except `trip_id` and `position` is nullable, and each absence means
    something: a part with dates and no place is a transit day or a never-geocoded week, a
    part with a place and no dates is what a migrated middle stop becomes, and a part with
    neither is identified by its ordinal alone.

    The place half is a value object snapshotted from the geocoder, not a shared entity:
    no public `uuid`, no ownership of its own, no global gazetteer behind it. A trip's
    parts are replaced wholesale on every write, so a row only ever means "this is what the
    geocoder said when the diver picked this place" - if the gazetteer later renames or
    moves it, the trip keeps the name the diver saw. A place the geocoder could not answer
    for is stored as a name with no coordinates, the free-text escape hatch that keeps a
    throttled or unreachable provider from blocking the diver.

    `position` preserves the order the diver arranged the parts in, not date order: the
    drag handle is what sets it, and an undated part has no place in a date ordering.
    Duplicate names are legal (two stays in the same town on one trip) and parts may
    overlap or leave gaps, so there is no unique constraint and no contiguity rule.

    The FK's `ON DELETE CASCADE` is the only way these rows are removed, and it fires:
    `erase_trip` is a real `DELETE FROM trip`, so a trip takes its parts with it. That is
    *not* the same as the dive join tables, whose cascade still never runs - `Dive` is
    soft-deleted, so no `DELETE FROM dive` is ever issued.
    """

    __tablename__ = "trip_part"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    # No standalone index on trip_id: the composite index below (leading column trip_id)
    # already serves lookups filtered by trip_id alone, plus satisfies the ORDER BY
    # position used by get_parts_for_trip/get_parts_for_trips and the correlated
    # `min(start_date)` the trip list orders by.
    trip_id: Mapped[int] = mapped_column(ForeignKey("trip.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    start_date: Mapped[date | None] = mapped_column(Date, default=None)
    end_date: Mapped[date | None] = mapped_column(Date, default=None)
    # Nullable, unlike the column this table replaces: a part with no place has no name,
    # and naming it after the trip would invent a place the diver never picked.
    name: Mapped[str | None] = mapped_column(String(255), default=None)
    display_name: Mapped[str | None] = mapped_column(String(512), default=None)
    latitude: Mapped[float | None] = mapped_column(Float, default=None)
    longitude: Mapped[float | None] = mapped_column(Float, default=None)
    bbox_south: Mapped[float | None] = mapped_column(Float, default=None)
    bbox_north: Mapped[float | None] = mapped_column(Float, default=None)
    bbox_west: Mapped[float | None] = mapped_column(Float, default=None)
    bbox_east: Mapped[float | None] = mapped_column(Float, default=None)

    __table_args__ = (Index("ix_trip_part_trip_id_position", "trip_id", "position"),)
