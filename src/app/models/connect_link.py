from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class ConnectLink(Base):
    """A member's standing connect link, `/connect/<token>`: whoever follows it may put the
    member in their people, connected, and on an invite-only instance may create an account
    here, counted as that member's invitation and charged to their quota.

    **The token is stored in clear**, unlike a check-in link's: the member's pane shows the
    same link and QR code on every visit, which a hash could not. What it reaches is the
    member's name and picture, a tie to them, and on an invite-only instance a stranger under
    their quota, until the member resets or removes it.

    One row per member, which the unique `user_id` enforces. A reset replaces `token` in place
    and a remove deletes the row, so there is nothing to expire or sweep. No public `uuid`: the
    member reaches the row through `/user/connect-link`, and anyone else through the token.
    """

    __tablename__ = "connect_link"

    id: Mapped[int] = mapped_column(autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), unique=True, index=True)
    # Within `JOIN_CHANNEL_SLUG_PATTERN` by construction, since it rides the same `via` field a
    # join-channel slug does - see `services.connect_links.new_connect_link_token`.
    token: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    # When the current token was made: a reset refreshes it.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
