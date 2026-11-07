from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, TimestampMixin


class DiveSiteProposal(Base, PublicUUIDMixin, TimestampMixin):
    """One account asking for a change to OpenDiving Commons, and what became of it.

    Private to the account and the reviewer, and tied to the account: both keys cascade, so
    deleting the account deletes its proposals whatever their status, and deleting a private
    site deletes those on it. A record that was accepted stays, since it is a `dive_site` row
    this table only pointed at.
    """

    __tablename__ = "dive_site_proposal"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    # The site proposed or changed. Once accepted, the Commons record it became or was folded
    # into, so a proposer whose own row has gone still reads the outcome.
    dive_site_id: Mapped[int] = mapped_column(ForeignKey("dive_site.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    # The shared members as the proposer asked for them: every one for a new site, following the
    # site's edits while it waits, and for a change only those they wanted different. Nothing a
    # Commons record would not carry is ever written here.
    members: Mapped[dict[str, Any]] = mapped_column(JSON)
    # The words the proposer affirmed and when: the proposal's evidence of the CC0 dedication.
    attestation: Mapped[str] = mapped_column(Text)
    attested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="waiting")
    message: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    __table_args__ = (
        # One waits per account and site, which is what proposing again replaces.
        Index(
            "ux_dive_site_proposal_one_waiting",
            "user_id",
            "dive_site_id",
            unique=True,
            postgresql_where=text("status = 'waiting'"),
        ),
        CheckConstraint("kind IN ('new_site', 'change')", name="ck_dive_site_proposal_kind"),
        CheckConstraint(
            "status IN ('waiting', 'accepted', 'declined', 'withdrawn')", name="ck_dive_site_proposal_status"
        ),
        CheckConstraint("status <> 'declined' OR reason IS NOT NULL", name="ck_dive_site_proposal_declined_has_reason"),
    )
