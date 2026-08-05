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

    # Nullable because Google-only accounts (see `/login/google`) never set a
    # password - there's nothing to hash. `authenticate_user` (core/security.py)
    # treats a `None` here as "password sign-in unavailable for this account".
    hashed_password: Mapped[str | None] = mapped_column(String, default=None)

    # The `sub` claim from Google's ID token, i.e. the stable, unique identifier
    # for the Google account - set once an account has signed in with Google at
    # least once (either created via Google, or a password account that later
    # links its already-verified email to a Google account). Never reused across
    # users, hence `unique=True`.
    google_id: Mapped[str | None] = mapped_column(String, unique=True, index=True, default=None)

    profile_image_url: Mapped[str] = mapped_column(String, default="https://profileimageurl.com")
    is_superuser: Mapped[bool] = mapped_column(default=False)

    # Overrides `SoftDeleteMixin.is_deleted` to add an index: unlike Dive/Trip/DiveSite
    # (each of which has a compound partial index that already covers `is_deleted` as a
    # leading/predicate column), no other index on this table covers it.
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True, kw_only=True)
