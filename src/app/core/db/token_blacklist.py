from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class TokenBlacklist(Base):
    __tablename__ = "token_blacklist"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    token: Mapped[str] = mapped_column(String, unique=True, index=True)
    # Timezone-aware, like every other timestamp in the schema: `core.security._blacklist_one`
    # writes `datetime.fromtimestamp(exp, UTC)` and `purge_expired_tokens` compares against
    # `datetime.now(UTC)`, and asyncpg refuses to bind an aware datetime to a naive
    # `TIMESTAMP WITHOUT TIME ZONE` column at all.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # When the row was written, which is a different question from `expires_at` - that one
    # is copied from the token's own `exp` and so says when it was *issued*. Only this
    # column can answer "how long after it was spent was this token presented again?", the
    # one signal that separates rotation's documented two-tab race (milliseconds) from a
    # stolen cookie being replayed (minutes or hours) - and so, since that separation is
    # what a session revocation now hangs off, the column a stolen cookie's blast radius is
    # measured against. Read by `api.v1.auth._handle_revoked_refresh` and by nothing else.
    #
    # `server_default` for the same reason `user.units` has one: the column is NOT NULL, so
    # the migration adding it needs a server-side default to backfill existing rows. Every
    # row written since is stamped explicitly by `core.security._blacklist_one`.
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
