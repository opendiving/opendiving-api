import uuid as uuid_pkg
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, MappedAsDataclass, mapped_column
from uuid6 import uuid7


class PublicUUIDMixin(MappedAsDataclass):
    """Adds an opaque, uuid7-based `uuid` column - unique and indexed, but distinct
    from the primary key - meant to be exposed as a model's public identifier (e.g. in
    URLs) instead of the internal sequential `id`.

    `kw_only=True` keeps this field from disturbing the positional field order of
    whatever model mixes it in: since it always has a default, it would otherwise have
    to precede every non-default field in the generated dataclass `__init__` (which,
    coming from a mixin, isn't guaranteed).
    """

    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), default_factory=uuid7, unique=True, index=True, kw_only=True
    )


class TimestampMixin(MappedAsDataclass):
    """Adds timezone-aware `created_at`/`updated_at` columns.

    See `PublicUUIDMixin` for why these are `kw_only`.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC), kw_only=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None, kw_only=True)


class SoftDeleteMixin(MappedAsDataclass):
    """Adds `deleted_at`/`is_deleted` columns; FastCRUD's `.delete()` sets these instead
    of issuing a `DELETE FROM` when a model supports soft deletion.

    See `PublicUUIDMixin` for why these are `kw_only`. Models whose soft-delete flag
    needs its own index (rather than being covered by another compound/partial index)
    should override `is_deleted` locally with `index=True`.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None, kw_only=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, kw_only=True)
