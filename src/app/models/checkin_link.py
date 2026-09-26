from datetime import UTC, date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class CheckinLink(Base):
    """A diver's check-in page, shared as a link: whoever holds the token sees the page as it
    prints, until `expires_at` or a revoke.

    Only the token's SHA-256 is stored, as for a magic link, so no row holds the credential.
    Minting revokes the diver's other links, so one is live at a time, and the hourly sweep
    deletes expired and revoked rows. No public `uuid`: the diver reaches their link through
    `/user/checkin-link`, and a desk through the token.

    The diving figures are the ones the page showed when the link was minted, the diver's
    correction included, and nothing moves them afterwards. Each is nullable: a null prints
    nothing.
    """

    __tablename__ = "checkin_link"

    id: Mapped[int] = mapped_column(autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Indexed for the sweep's predicate, as `user_session.expires_at` is.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # The types of the `user_dive_stats` columns the logged figures come from, so no logged
    # figure can be refused here.
    total_dives: Mapped[int | None] = mapped_column(Integer)
    max_depth: Mapped[float | None] = mapped_column(Float)
    last_dive_on: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
