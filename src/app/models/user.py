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
    # `Mapped[bool]` without `| None` deliberately - the column is NOT NULL, and that is
    # what the `default`/`server_default` pair is for. `default=` is client-side, applied
    # by SQLAlchemy on INSERT, so it never reaches the DDL and Alembic cannot see it;
    # only `server_default` gives the migration adding this column a value to backfill
    # the rows already in the table with, which a NOT NULL column has to have.
    gear_service_emails: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    # Which measurement system this diver reads and types in - `metric` or `imperial`
    # (`UnitSystem` in `schemas/user.py` is the vocabulary; the column is a plain
    # `VARCHAR`, the `GearItem.type` shape). Stored server-side rather than per device
    # so every client the diver signs into agrees, and nothing the API serves varies by
    # it: measurements are metric everywhere and this says who is looking (see
    # DECISIONS.md).
    #
    # Same `default`/`server_default` pair as `gear_service_emails` above, for the same
    # reason - the column is NOT NULL, so the migration adding it needs a server-side
    # default to backfill the existing rows.
    units: Mapped[str] = mapped_column(String(16), default="metric", server_default="metric")

    # Overrides `SoftDeleteMixin.is_deleted` to add an index: unlike Dive, Certification and
    # GearServiceRecord (each of which has a compound partial index whose predicate already
    # pins `is_deleted`), no other index on this table covers it. Trip and DiveSite used to
    # be on that list and are hard-deleted now, so they have no such column to cover.
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True, kw_only=True)
