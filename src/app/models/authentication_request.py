from datetime import UTC, datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class AuthenticationRequest(Base):
    """A single-use, short-lived email magic-link token.

    Deliberately separate from `User`/`AuthenticationProvider`: proving ownership of an
    email address must never, by itself, create a user record - see `POST
    /auth/email/verify`, which only issues a temporary onboarding session (a signed JWT,
    never persisted) for emails with no existing account, and defers actual account
    creation to profile completion (`POST /auth/complete`).
    """

    __tablename__ = "authentication_request"

    id: Mapped[int] = mapped_column(autoincrement=True, nullable=False, unique=True, primary_key=True, init=False)
    email: Mapped[str] = mapped_column(String(50), index=True)

    # SHA-256 hex digest of the raw token emailed to the user - only the hash is ever
    # persisted, so a DB leak alone can't be used to mint valid magic links.
    token_hash: Mapped[str] = mapped_column(String, unique=True, index=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )
