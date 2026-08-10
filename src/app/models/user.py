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

    # No password/provider-id columns here at all - a user's actual authentication
    # methods (magic-link email, Google, and any future provider) live exclusively in
    # `AuthenticationProvider`, one row per linked provider. This is what lets the same
    # account be reached via either method without the `User` row itself needing to
    # know which ones are in use.
    profile_image_url: Mapped[str] = mapped_column(String, default="https://profileimageurl.com")
    is_superuser: Mapped[bool] = mapped_column(default=False)

    # Whether to email this user when their gear is due for servicing (see
    # `core.worker.functions.send_gear_service_digests`). Opt-*out* rather than opt-in:
    # a reminder nobody switched on is a reminder that never arrives, and the whole point
    # of the feature is reaching a diver who isn't currently in the app.
    #
    # `Mapped[bool]` without `| None` deliberately - the column is NOT NULL, and
    # `server_default` is what makes the model agree with the hand-written ALTER TABLE
    # that adds it to an existing database (see DECISIONS.md; `create_all` never alters
    # an existing table).
    gear_service_emails: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    # Overrides `SoftDeleteMixin.is_deleted` to add an index: unlike Dive/Trip/DiveSite
    # (each of which has a compound partial index that already covers `is_deleted` as a
    # leading/predicate column), no other index on this table covers it.
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True, kw_only=True)
