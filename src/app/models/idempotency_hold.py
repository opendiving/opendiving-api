import uuid as uuid_pkg
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, SmallInteger, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base

MAX_IDEMPOTENCY_KEY_LENGTH = 255


class IdempotencyHold(Base):
    """One create performed under an `Idempotency-Key`: which account sent it to which path,
    a digest of what it sent, and the uuid of the record it made.

    Claimed at the start of the create's transaction and committed with the record, so one
    never exists without the other (`api.idempotency`). It names the record rather than
    holding the response: a replay re-reads the record, and nothing of it is copied here.
    Swept `EXTENDED_REFRESH_TOKEN_EXPIRE_DAYS` after it was made, and gone with its account.
    """

    __tablename__ = "idempotency_hold"
    __table_args__ = (Index("ux_idempotency_hold_user_id_route_key", "user_id", "route", "key", unique=True),)

    id: Mapped[int] = mapped_column(autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    # The request's path rather than its template, so a key reused on another route - or on
    # another record's sub-route - is another hold.
    route: Mapped[str] = mapped_column(String(255))
    key: Mapped[str] = mapped_column(String(MAX_IDEMPOTENCY_KEY_LENGTH))
    # SHA-256 of the request body's bytes as received, hex.
    request_digest: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int] = mapped_column(SmallInteger)
    # Null only between the claim and the record's flush, inside one transaction: the commit
    # refuses a hold that has not named its record.
    record_uuid: Mapped[uuid_pkg.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    # Indexed for the sweep's predicate.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC), index=True
    )
