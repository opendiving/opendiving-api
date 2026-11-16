from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin


class DiveSiteHistory(Base, PublicUUIDMixin):
    """One accepted change to a Commons record: a diver's proposal as they asked it, or a
    reviewer's correction of it. Written with the record and never rewritten.

    It names nobody itself. The proposer is reached through the proposal and the reviewer
    through a link of its own, and both empty when their account is purged, so the entry
    outlives every account it was about.
    """

    __tablename__ = "dive_site_history"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    dive_site_id: Mapped[int] = mapped_column(ForeignKey("dive_site.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    # The acceptance's moment, shared by every entry it writes.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Each shared member that differs, `{field, old, new}`, as the shared-members read
    # serialises the values. Empty on an `added` entry.
    details: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    # Set on a diver's entry only, while the proposal lasts.
    proposal_id: Mapped[int | None] = mapped_column(
        ForeignKey("dive_site_proposal.id", ondelete="SET NULL"), index=True, default=None
    )
    # Who accepted, on every entry of the acceptance, while that account lasts.
    reviewer_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), index=True, default=None
    )
    # Whether the review corrected the diver's change: a boolean on a diver's entry and null on
    # a reviewer's correction, which is how the two are told apart once both links are empty.
    corrected: Mapped[bool | None] = mapped_column(Boolean, default=None)

    __table_args__ = (
        # Newest first, a reviewer's correction above the diver's entry it corrected.
        Index("ix_dive_site_history_dive_site_id_created_at", "dive_site_id", "created_at", "id"),
        CheckConstraint("kind IN ('added', 'changed')", name="ck_dive_site_history_kind"),
        CheckConstraint("corrected IS NOT NULL OR proposal_id IS NULL", name="ck_dive_site_history_correction"),
        CheckConstraint("corrected IS NOT NULL OR kind = 'changed'", name="ck_dive_site_history_correction_kind"),
    )
