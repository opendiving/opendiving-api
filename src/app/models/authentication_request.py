from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class AuthenticationRequest(Base):
    """A short-lived email magic-link token.

    Deliberately separate from `User`/`AuthenticationProvider`: proving ownership of an
    email address must never, by itself, create a user record - see `POST
    /auth/email/verify`, which only issues a temporary onboarding session (a signed JWT,
    never persisted) for emails with no existing account, and defers actual account
    creation to profile completion (`POST /auth/complete`).

    Also doubles as the magic-link backing an *existing* user's email-change
    confirmation (`purpose="email_change"`, see `POST /user/email-change/request`/
    `POST /user/email-change/verify` in `api.v1.users`) - the mechanics (hashed token,
    short expiry) are identical, only what "verifying" it does differs.
    """

    __tablename__ = "authentication_request"

    id: Mapped[int] = mapped_column(autoincrement=True, nullable=False, primary_key=True, init=False)

    # For `purpose="sign_in"`, the (unverified-until-now) email being signed in with.
    # For `purpose="email_change"`, the *new* address `user_id` wants to change to.
    email: Mapped[str] = mapped_column(String(50), index=True)

    # SHA-256 hex digest of the raw token emailed to the user - only the hash is ever
    # persisted, so a DB leak alone can't be used to mint valid magic links.
    token_hash: Mapped[str] = mapped_column(String, unique=True, index=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # Timestamp of the first successful verification, and what makes a token
    # single-use - though "used" means something slightly different per `purpose`.
    # `"sign_in"` rejects a replay outright: `verify_email_link` mints a refresh
    # cookie, so a repeat hands out a whole new session rather than re-confirming
    # the old one. `"email_change"` still tolerates one, but only while the change
    # this token represents is the account's *current* email - re-applying that
    # grants nothing. See `invalidated_at` for the separate case of a token revoked
    # by a newer request superseding it. Stamped only through
    # `crud_authentication_requests.claim_authentication_request`, whose conditional
    # UPDATE is what holds that "single-use" up when two verifications race.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # Set when a *newer* request supersedes this one (see `POST /auth/email/request`/
    # `POST /user/email-change/request`, which invalidate any still-live previous
    # request for the same email/user) - this is what actually revokes a token,
    # distinct from `used_at`.
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # "sign_in" (the original magic-link flow) or "email_change". Determines which
    # endpoint is willing to consume a given row, and what "used" means for it.
    purpose: Mapped[str] = mapped_column(String(20), default="sign_in")

    # Only set for `purpose="email_change"` - the already-existing user requesting the
    # change. `None` for `purpose="sign_in"`, since that flow is deliberately usable
    # before any `User` row exists at all.
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
