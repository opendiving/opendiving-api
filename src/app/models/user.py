from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, SoftDeleteMixin, TimestampMixin


class User(Base, PublicUUIDMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(autoincrement=True, primary_key=True, init=False)

    name: Mapped[str] = mapped_column(String(30))
    username: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String)

    profile_image_url: Mapped[str] = mapped_column(String, default="https://profileimageurl.com")
    is_superuser: Mapped[bool] = mapped_column(default=False)

    # Overrides `SoftDeleteMixin.is_deleted` to add an index: unlike Dive/Trip/DiveSite
    # (each of which has a compound partial index that already covers `is_deleted` as a
    # leading/predicate column), no other index on this table covers it.
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True, kw_only=True)
