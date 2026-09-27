from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class TripPerson(Base):
    """The people who came on a trip - `DivePerson`'s shape, for a trip.

    Who came on the trip is its own fact, not a walk of its dives: a companion who never
    dived is on the trip and on none of them.
    """

    __tablename__ = "trip_person"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    trip_id: Mapped[int] = mapped_column(ForeignKey("trip.id", ondelete="CASCADE"))
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str | None] = mapped_column(String(16), default=None)

    __table_args__ = (
        UniqueConstraint("trip_id", "person_id", name="ux_trip_person_trip_id_person_id"),
        Index("ix_trip_person_trip_id_position", "trip_id", "position"),
    )
