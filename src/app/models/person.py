from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from ..core.db.database import Base
from ..core.db.models import CustomPropertiesMixin, PublicUUIDMixin, TimestampMixin


class Person(Base, PublicUUIDMixin, TimestampMixin, CustomPropertiesMixin):
    """An individual the diver was with - a buddy, a guide, an instructor, a fellow student,
    a companion who stayed on the boat - referenced with a role per occasion from dives,
    trips and courses, and from a certification as its instructor.

    The diver's own record about somebody, as a contact is about a party: the name is what
    the diver wrote, and two divers' records of one person are two rows. `connected_user_id`
    may name the account on this instance the person is. The tie shows the diver that
    account's picture and current username, and shows the account the diver - name, username
    and picture - under its *Added you*, until either side cuts it. A tie is made by the
    account's connect link, by adding back an account that has the diver in its people, by an
    import, or at sign-up through a link; no column says which.

    `connection_hidden_at` is the connected account's Hide: it sits on the tie it hides, so a
    tie cut and made afresh shows again.

    No `SoftDeleteMixin`: deleting a person removes the row and its references with it.
    """

    __tablename__ = "person"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    # One of the two foreign keys into `user.id` that are not a cascade: it names *another*
    # account, whose purge must disconnect this diver's record rather than delete it. See
    # *"Every foreign key into `user.id` declares `ondelete="CASCADE"` but two"* in DECISIONS.md.
    connected_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), default=None, index=True
    )
    email: Mapped[str | None] = mapped_column(String(255), default=None)
    phone: Mapped[str | None] = mapped_column(String(32), default=None)
    # `{"url", "label"}` objects (`schemas/link.py`), in the diver's order, possibly none.
    links: Mapped[list[dict[str, str | None]]] = mapped_column(JSON, default_factory=list, server_default="[]")
    notes: Mapped[str] = mapped_column(Text, default="")
    # Every write that cuts or makes a tie writes this null with it, the purge included, whose
    # `SET NULL` clears the key alone. No check ties the two: it would refuse that `SET NULL`
    # for an account deleted by anything but the purge.
    connection_hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    @declared_attr.directive
    @classmethod
    def __table_args__(cls) -> tuple:
        return (
            # See *"Case-insensitive per-user uniqueness"* in DECISIONS.md. Names are stored
            # trimmed, so `lower(name)` is the trimmed, case-folded key.
            Index("ux_person_user_id_name_lower", "user_id", func.lower(cls.name), unique=True),
            # Serves `GET /people`: `WHERE user_id = ... ORDER BY name`.
            Index("ix_person_user_id_name", "user_id", "name"),
            # One person per connected account in a diver's list.
            Index(
                "ux_person_user_id_connected_user_id",
                "user_id",
                "connected_user_id",
                unique=True,
                postgresql_where=cls.connected_user_id.is_not(None),
            ),
            CheckConstraint("connected_user_id <> user_id", name="ck_person_not_connected_to_its_owner"),
        )
