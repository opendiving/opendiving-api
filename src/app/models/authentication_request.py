from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin


class AuthenticationRequest(Base, PublicUUIDMixin):
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

    One `purpose="sign_in"` row backs *two* credentials, not one: the link in the email
    and the six-digit code printed beside it (`code_hash`). Either completes the sign-in
    and whichever is used first consumes the row, because both end at the same
    `claim_authentication_request`. They are deliberately unequal in strength, and
    `code_hash`'s comment says how that asymmetry is contained.
    """

    __tablename__ = "authentication_request"

    id: Mapped[int] = mapped_column(autoincrement=True, nullable=False, primary_key=True, init=False)

    # For `purpose="sign_in"`, the (unverified-until-now) email being signed in with.
    # For `purpose="email_change"`, the *new* address `user_id` wants to change to.
    email: Mapped[str] = mapped_column(String(50), index=True)

    # SHA-256 hex digest of the raw token emailed to the user - only the hash is ever
    # persisted, so a DB leak alone can't be used to mint valid magic links.
    token_hash: Mapped[str] = mapped_column(String, unique=True, index=True)

    # Indexed for the hourly sweep's `WHERE expires_at < :cutoff`
    # (`core.worker.functions.purge_expired_authentication_requests`), which is the
    # only thing that has ever removed a row from this table - and the table it has to
    # get through is the one that grew unbounded until that job existed.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    # SHA-256 hex digest of the six-digit sign-in code printed in the same email as the
    # link (`POST /auth/email/verify-code`). Only ever set for `purpose="sign_in"`: an
    # email change is confirmed by opening the link in the new mailbox, and a code typed
    # into the tab that asked would prove nothing about that mailbox.
    #
    # The hash is hygiene, not protection. Six digits is a space an offline attacker
    # walks in milliseconds, so what actually defends the code is `code_attempts` below;
    # hashing only keeps a live credential out of logs, dumps and admin-panel views, the
    # same reason `token_hash` is a hash.
    #
    # `NULL` also means "spent": nulled once `code_attempts` reaches
    # `SIGN_IN_CODE_ATTEMPTS_MAX`, which kills the code while deliberately leaving the
    # link in this same row alive - see `register_failed_code_attempt`.
    code_hash: Mapped[str | None] = mapped_column(String(64), default=None)

    # Wrong guesses against `code_hash` so far. Incremented in the same statement that
    # reads it, never by a read followed by a write - see `register_failed_code_attempt`.
    code_attempts: Mapped[int] = mapped_column(Integer, default=0)

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
