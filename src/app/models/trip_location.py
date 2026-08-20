from sqlalchemy import Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class TripLocation(Base):
    """A place a trip went to, snapshotted from the geocoder at the time it was picked.

    These are value objects, not shared entities: no public `uuid`, no ownership of
    their own, no global gazetteer behind them. A trip's locations are replaced
    wholesale on every write, so a row only ever means "this is what the geocoder said
    when the diver picked this place" - if the gazetteer later renames or moves it, the
    trip keeps the name the diver saw.

    `position` preserves the order the places were listed in - 0 is the first, shown
    wherever only one location fits. Duplicate names are legal (two stays in the same
    town on one trip), so there is no unique constraint.

    A location the geocoder could not answer for is stored as a name with no
    coordinates - the free-text escape hatch that keeps a throttled or unreachable
    provider from blocking the diver.

    The FK's `ON DELETE CASCADE` is the only way these rows are removed, and it fires:
    `erase_trip` is a real `DELETE FROM trip`, so a trip takes its places with it. That is
    *not* the same as the dive join tables, whose cascade still never runs - `Dive` is
    soft-deleted, so no `DELETE FROM dive` is ever issued.
    """

    __tablename__ = "trip_location"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    # No standalone index on trip_id: the composite index below (leading column trip_id)
    # already serves lookups filtered by trip_id alone, plus satisfies the ORDER BY
    # position used by get_locations_for_trip/get_locations_for_trips.
    trip_id: Mapped[int] = mapped_column(ForeignKey("trip.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    position: Mapped[int] = mapped_column(Integer, default=0)
    display_name: Mapped[str | None] = mapped_column(String(512), default=None)
    latitude: Mapped[float | None] = mapped_column(Float, default=None)
    longitude: Mapped[float | None] = mapped_column(Float, default=None)
    bbox_south: Mapped[float | None] = mapped_column(Float, default=None)
    bbox_north: Mapped[float | None] = mapped_column(Float, default=None)
    bbox_west: Mapped[float | None] = mapped_column(Float, default=None)
    bbox_east: Mapped[float | None] = mapped_column(Float, default=None)

    __table_args__ = (Index("ix_trip_location_trip_id_position", "trip_id", "position"),)
