from datetime import datetime

from sqlalchemy import DateTime, String
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
