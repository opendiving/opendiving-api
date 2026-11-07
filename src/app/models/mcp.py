"""Durable delegated credentials. Raw codes and bearer secrets never enter storage."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin


class McpClient(Base):
    __tablename__ = "mcp_client"

    client_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))


class McpGrant(Base, PublicUUIDMixin):
    __tablename__ = "mcp_grant"

    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("mcp_client.client_id", ondelete="CASCADE"), index=True)
    scopes: Mapped[str] = mapped_column(String(4096))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class McpAuthorization(Base):
    __tablename__ = "mcp_authorization"

    secret_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("mcp_client.client_id", ondelete="CASCADE"), index=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # NULL until consent. A code and its preceding consent request are distinct rows.
    grant_id: Mapped[int | None] = mapped_column(
        ForeignKey("mcp_grant.id", ondelete="CASCADE"), index=True, default=None
    )


class McpToken(Base):
    __tablename__ = "mcp_token"

    secret_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    grant_id: Mapped[int] = mapped_column(ForeignKey("mcp_grant.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(8))
    scopes: Mapped[str] = mapped_column(String(4096))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    spent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class McpAudit(Base, PublicUUIDMixin):
    __tablename__ = "mcp_audit"

    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    grant_id: Mapped[int] = mapped_column(ForeignKey("mcp_grant.id", ondelete="CASCADE"), index=True)
    tool: Mapped[str] = mapped_column(String(128))
    status: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))


class McpFile(Base, PublicUUIDMixin):
    __tablename__ = "mcp_file"

    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    grant_id: Mapped[int] = mapped_column(ForeignKey("mcp_grant.id", ondelete="CASCADE"), index=True)
    storage_key: Mapped[str] = mapped_column(String(512))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(128))
    byte_size: Mapped[int]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
