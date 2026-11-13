from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, TimestampMixin


class SpeciesPhotoProposal(Base, PublicUUIDMixin, TimestampMixin):
    """One account saying a Commons file would be a better photo for a species, and what the
    operator made of it.

    Both keys cascade, so purging the account deletes its proposals whatever their status - a
    declined one included, which is the memory that bars its file from waiting again. A photo
    pinned from an accepted one stays, since it lives on the `species` row.
    """

    __tablename__ = "species_photo_proposal"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    species_id: Mapped[int] = mapped_column(ForeignKey("species.id", ondelete="CASCADE"), index=True)
    # The Commons file title as `species_photos.normalize_file_title` spells it, the spelling the
    # stored `photo_file` and a pin use, so the three compare as equal strings.
    file: Mapped[str] = mapped_column(String(255))
    note: Mapped[str] = mapped_column(Text, default="")
    # `closed` is the operator choosing the photo another way - a pin of a different file, or a
    # hide - without ruling on this file, which is why only `declined` bars it.
    status: Mapped[str] = mapped_column(String(16), default="waiting")
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    __table_args__ = (
        # One waits per account and species, which is what proposing again replaces.
        Index(
            "ux_species_photo_proposal_one_waiting",
            "user_id",
            "species_id",
            unique=True,
            postgresql_where=text("status = 'waiting'"),
        ),
        CheckConstraint(
            "status IN ('waiting', 'accepted', 'declined', 'withdrawn', 'closed')",
            name="ck_species_photo_proposal_status",
        ),
    )
