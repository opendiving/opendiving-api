from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin


class LogbookImport(Base, PublicUUIDMixin):
    """An import the server holds: a pending one's files until it is applied or discarded, and
    its review and its log for as long as the history keeps them. One `pending` per account,
    finished from any device; an upload applied at once is stored `applied` and holds nothing.

    `review` and `notes` are what the routes answer with, as they were stored and never
    validated again; `review_shape` says which shape that is (`services/logbook_import/imports.py`).
    `remaps` and `sources` are the second pass's, empty where there was none, and are never served.

    Hard-deleted, by `DELETE /import/{uuid}`, the hourly purge past `expires_at`, or with the
    account.
    """

    __tablename__ = "logbook_import"
    __table_args__ = (
        # One waits per account, which an upload racing another's is refused on.
        Index("ux_logbook_import_one_pending", "user_id", unique=True, postgresql_where=text("status = 'pending'")),
        # Serves `GET /imports`: `WHERE user_id = ... AND status = 'applied' ORDER BY applied_at DESC`.
        Index("ix_logbook_import_user_id_status_applied_at", "user_id", "status", "applied_at"),
        Index("ix_logbook_import_expires_at", "expires_at"),
        CheckConstraint("status IN ('pending', 'applied')", name="ck_logbook_import_status"),
    )

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    review_shape: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Null where the setting that bounds it was blank when this import reached its state.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    review: Mapped[dict[str, Any]] = mapped_column(JSON, default_factory=dict)
    notes: Mapped[dict[str, Any]] = mapped_column(JSON, default_factory=dict)
    # The dive rows by outcome, so the history lists without parsing every listed review.
    counts: Mapped[dict[str, int]] = mapped_column(JSON, default_factory=dict)
    remaps: Mapped[dict[str, Any]] = mapped_column(JSON, default_factory=dict)
    sources: Mapped[list[Any]] = mapped_column(JSON, default_factory=list)


class LogbookImportFile(Base):
    """One file a pending import holds: a readable file of the upload, or the portrait its
    review offers. Its bytes are in the blob store, and it goes when the import is applied."""

    __tablename__ = "logbook_import_file"
    __table_args__ = (CheckConstraint("role IN ('part', 'portrait')", name="ck_logbook_import_file_role"),)

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    logbook_import_id: Mapped[int] = mapped_column(ForeignKey("logbook_import.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    filename: Mapped[str] = mapped_column(Text)
    byte_size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(255), unique=True)
    # The upload's `file` part this came from, which a member row's `part` names; a zip's
    # members share theirs. Null on the portrait.
    part: Mapped[int | None] = mapped_column(Integer, default=None)
