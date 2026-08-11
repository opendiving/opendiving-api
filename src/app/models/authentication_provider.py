from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class AuthenticationProvider(Base):
    """Links a `User` to one identity provider they've authenticated with - e.g.
    `provider="email"` for the magic-link flow, or `provider="google"` with
    `provider_user_id` set to Google's `sub` claim.

    A user can have several of these rows (one per linked provider), which is what
    lets the same account be signed into via either email or Google - or, in the
    future, Apple/GitHub/Microsoft/etc. - without the `User` row itself needing to
    change at all when a new provider is added or linked.
    """

    __tablename__ = "authentication_provider"
    __table_args__ = (
        # A given provider identity (e.g. one specific Google account) can only ever
        # be linked to a single user. Postgres treats NULLs as distinct from one
        # another for uniqueness purposes, so this doesn't stop multiple "email" rows
        # (which never set `provider_user_id`) from coexisting.
        UniqueConstraint("provider", "provider_user_id", name="uq_authentication_provider_provider_identity"),
        # A user can only link a given provider once (no duplicate "google" rows for
        # the same account).
        UniqueConstraint("user_id", "provider", name="uq_authentication_provider_user_provider"),
    )

    id: Mapped[int] = mapped_column(autoincrement=True, nullable=False, unique=True, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(20))

    # `None` for providers with no external account id of their own - "email" proves
    # ownership via the magic link itself rather than a stored identifier. Set for
    # "google" (Google's `sub` claim), and future providers like "apple"/"github".
    provider_user_id: Mapped[str | None] = mapped_column(String, index=True, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
