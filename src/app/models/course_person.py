from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class CoursePerson(Base):
    """The people on a course - its instructors among them, by role - in `DivePerson`'s
    shape."""

    __tablename__ = "course_person"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    course_id: Mapped[int] = mapped_column(ForeignKey("course.id", ondelete="CASCADE"))
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str | None] = mapped_column(String(16), default=None)

    __table_args__ = (
        UniqueConstraint("course_id", "person_id", name="ux_course_person_course_id_person_id"),
        Index("ix_course_person_course_id_position", "course_id", "position"),
    )
