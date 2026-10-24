from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin


class DiveDraft(Base, PublicUUIDMixin):
    """A dive not yet logged: the dive form's values with nothing required, written from
    either device and finished from either.

    `values` is the form's state as JSON in `DiveCreateRequest`'s member names, validated
    by `schemas/dive_draft.py` member by member and never across members. The uuids inside
    it are not foreign keys, so nothing keeps them true; a read drops the ones that no
    longer name a record of the account's (`services/dive_drafts.py`). `gear_set_id` is the
    one reference a column holds, cleared when the set is deleted.

    Hard-deleted, by `DELETE /dive-draft/{uuid}` or by logging it, and with the account.
    """

    __tablename__ = "dive_draft"
    __table_args__ = (
        # Serves `GET /dive-drafts`: `WHERE user_id = ... ORDER BY updated_at DESC`.
        Index("ix_dive_draft_user_id_updated_at", "user_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    values: Mapped[dict[str, Any]] = mapped_column(JSON, default_factory=dict)
    gear_set_id: Mapped[int | None] = mapped_column(
        ForeignKey("gear_set.id", ondelete="SET NULL"), default=None, index=True
    )
    # Both set on every create, so the list's order never meets a null.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
