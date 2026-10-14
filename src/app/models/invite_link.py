from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class InviteLink(Base):
    """A member's standing invite link: whoever follows `/join?via=<token>` may create an
    account here, counted as that member's invitation and charged to their quota.

    **The token is stored in clear**, unlike a check-in link's: the member's Invitations card
    shows the same link and QR code on every visit, which a hash could not. What it admits is
    a stranger under one member's quota, until the member resets or removes it.

    One row per member, which the unique `user_id` enforces. A reset replaces `token` in place
    and a remove deletes the row, so there is nothing to expire or sweep. No public `uuid`: the
    member reaches the row through `/user/invite-link`, and a stranger through the token.
    """

    __tablename__ = "invite_link"

    id: Mapped[int] = mapped_column(autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), unique=True, index=True)
    # Within `JOIN_CHANNEL_SLUG_PATTERN` by construction, since it rides the same `via` field a
    # join-channel slug does - see `services.invite_links.new_invite_link_token`.
    token: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    # When the current token was made: a reset refreshes it.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
