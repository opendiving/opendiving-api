from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, TimestampMixin


class Person(Base, PublicUUIDMixin, TimestampMixin):
    """An individual the diver was with - a buddy, a guide, an instructor, a fellow student,
    a companion who stayed on the boat - referenced with a role per occasion from dives,
    trips and courses, and from a certification as its instructor.

    The diver's own record about somebody, as a contact is about a party: the name is what
    the diver wrote, and two divers' records of one person are two rows. `linked_user_id`
    may name the account on this instance the person is; the link is one-directional and
    tells that account nothing.

    No `SoftDeleteMixin`: deleting a person removes the row and its references with it.
    """

    __tablename__ = "person"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    # The one foreign key into `user.id` that is not a cascade: it names *another* account,
    # whose purge must unlink this diver's record rather than delete it. See *"Every foreign
    # key into `user.id` cascades but one"* in DECISIONS.md.
    linked_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), default=None, index=True
    )
    email: Mapped[str | None] = mapped_column(String(255), default=None)
    phone: Mapped[str | None] = mapped_column(String(32), default=None)
    notes: Mapped[str] = mapped_column(Text, default="")

    @declared_attr.directive
    @classmethod
    def __table_args__(cls) -> tuple:
        return (
            # See *"Case-insensitive per-user uniqueness"* in DECISIONS.md. Names are stored
            # trimmed, so `lower(name)` is the trimmed, case-folded key.
            Index("ux_person_user_id_name_lower", "user_id", func.lower(cls.name), unique=True),
            # Serves `GET /people`: `WHERE user_id = ... ORDER BY name`.
            Index("ix_person_user_id_name", "user_id", "name"),
            # One person per linked account in a diver's list.
            Index(
                "ux_person_user_id_linked_user_id",
                "user_id",
                "linked_user_id",
                unique=True,
                postgresql_where=cls.linked_user_id.is_not(None),
            ),
            CheckConstraint("linked_user_id <> user_id", name="ck_person_not_linked_to_its_owner"),
        )
