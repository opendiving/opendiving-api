from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin


class ApiKey(Base, PublicUUIDMixin):
    """A diver's own tool's credential: an opaque secret on `Authorization: Bearer`, which
    `get_current_user` resolves to the account by the secret's hash.

    Its own table rather than a `user_session` row, because a session is what the devices
    card lists, what the live-session cap evicts and what the active-account counts read,
    and a nightly cron is none of those. Hard-deleted on revoke, like a passkey: every
    failure a key can have is the same 401, so a kept row would carry no reason anyone reads.
    """

    __tablename__ = "api_key"

    id: Mapped[int] = mapped_column(autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    # A label the diver chose with the secret, bounded like a passkey's, and not unique: two
    # tools may be "laptop".
    name: Mapped[str] = mapped_column(String(50))
    # `core.security.API_KEY_SHOWN_LENGTH` characters of the secret in clear - `odk_` and
    # eight of the random part - so a list row, an email and a log line can name a key
    # without holding it.
    prefix: Mapped[str] = mapped_column(String(12))
    # The SHA-256 of the whole secret, which is all that is kept of it - the check-in link's
    # column exactly.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # `schemas.api_key.ApiKeyScope`, a plain `VARCHAR` like `user_session.idle_timeout`.
    scope: Mapped[str] = mapped_column(String(16))
    # Null for a key made to last; past it, the key is refused and stays listed until revoked.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
