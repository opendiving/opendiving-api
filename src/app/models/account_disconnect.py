from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class AccountDisconnect(Base):
    """An account's Disconnect of a diver who had it in their people: `user_id` cut
    `disconnected_user_id`'s tie to it.

    Read by an import alone, which does not connect the disconnected diver's person to this
    account again. Removed when that diver connects to the account by its link or by adding it
    back, either of which is the account's consent; a cascade takes it with either account.
    """

    __tablename__ = "account_disconnect"

    id: Mapped[int] = mapped_column(autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    disconnected_user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))

    __table_args__ = (
        Index("ux_account_disconnect_user_id_disconnected_user_id", "user_id", "disconnected_user_id", unique=True),
    )
