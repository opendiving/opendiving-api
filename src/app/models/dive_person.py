from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class DivePerson(Base):
    """Join table naming the people on a dive, each with the role they had on it.

    `role` is one value of `PersonRole` (`schemas/person.py`) or null for *was there*; a
    plain string with no `CHECK`, like every stored vocabulary here. `position` keeps the
    diver's order. `TripPerson` and `CoursePerson` are the same shape for their hosts.

    Both cascades fire in one direction only: a person's delete takes its rows, while a
    dive is soft-deleted and keeps them - which is why `dive_count` counts live dives.
    """

    __tablename__ = "dive_person"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    # No standalone index: both constraints below lead with it.
    dive_id: Mapped[int] = mapped_column(ForeignKey("dive.id", ondelete="CASCADE"))
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str | None] = mapped_column(String(16), default=None)

    __table_args__ = (
        UniqueConstraint("dive_id", "person_id", name="ux_dive_person_dive_id_person_id"),
        Index("ix_dive_person_dive_id_position", "dive_id", "position"),
    )
