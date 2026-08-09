from sqlalchemy import ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, SoftDeleteMixin, TimestampMixin


class GearSet(Base, PublicUUIDMixin, TimestampMixin, SoftDeleteMixin):
    """A named, reusable grouping of a user's gear items, e.g. "Sidemount",
    "Tech - trimix" or "Warm water rec".

    Sets exist purely as a shortcut for filling in a dive's gear list: selecting one
    in the dive form replaces the form's items with the set's, after which the diver
    can add/remove items for that dive without affecting the stored set. A `Dive`
    therefore holds no reference to a set at all - only to the resulting gear items.
    """

    __tablename__ = "gear_set"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, unique=True, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))

    @declared_attr.directive
    @classmethod
    def __table_args__(cls) -> tuple:
        return (
            # Case-insensitive uniqueness per user on name, ignoring soft-deleted sets -
            # same pattern as `dive_site`/`trip` (see DECISIONS.md).
            Index(
                "ux_gear_set_user_id_name_lower",
                "user_id",
                func.lower(cls.name),
                unique=True,
                postgresql_where=cls.is_deleted.is_(False),
            ),
            # Serves `read_gear_sets` (`GET /gear-sets`): `WHERE user_id = ... AND
            # is_deleted = false ORDER BY name ASC`.
            Index(
                "ix_gear_set_user_id_name",
                "user_id",
                "name",
                postgresql_where=cls.is_deleted.is_(False),
            ),
        )
