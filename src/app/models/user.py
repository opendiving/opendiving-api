from sqlalchemy import Boolean, Index, String, func
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, SoftDeleteMixin, TimestampMixin


class User(Base, PublicUUIDMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(autoincrement=True, primary_key=True, init=False)

    name: Mapped[str] = mapped_column(String(30))
    username: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(50), unique=True, index=True)

    # No password/provider-id columns here at all - a user's actual authentication
    # methods (magic-link email, Google, and any future provider) live exclusively in
    # `AuthenticationProvider`, one row per linked provider. This is what lets the same
    # account be reached via either method without the `User` row itself needing to
    # know which ones are in use.
    # The diver's avatar, or `NULL` for the initials fallback. Two columns rather than a
    # `user_avatar` table: this is a strictly 1:1 optional attribute with no metadata worth
    # keeping (the served type is always WebP, and byte size and original filename stop
    # meaning anything once the upload has been re-encoded - see
    # `services/user_avatars.py`), and `get_current_user` already selects every mapped
    # column, so a column rides along free where a table would cost a join on the hottest
    # dependency in the app.
    #
    # `avatar_storage_key` is where the bytes are on the files volume,
    # `user-avatars/{sha256[:2]}/{nonce}_{sha256}`, minted by `blob_store.new_key`. The
    # nonce is per write, deliberately not this row's uuid: the row survives replacement,
    # so a key derived from it could be re-minted after being retired and a post-commit
    # unlink could then destroy a live blob.
    avatar_storage_key: Mapped[str | None] = mapped_column(String(255), default=None)
    # Hex SHA-256 of the **stored** (normalized) bytes, not of what was uploaded. It is
    # the download route's `ETag`, and `UserRead` publishes it as the version token the
    # clients append as `?v=` - so it doubles as "does this account have a picture".
    avatar_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    is_superuser: Mapped[bool] = mapped_column(default=False)

    # Whether to email this user when their gear is due for servicing (see
    # `core.worker.functions.send_gear_service_digests`). Opt-*out* rather than opt-in:
    # a reminder nobody switched on is a reminder that never arrives, and the whole point
    # of the feature is reaching a diver who isn't currently in the app.
    #
    # `Mapped[bool]` without `| None` deliberately - the column is NOT NULL, and that is
    # what the `default`/`server_default` pair is for. `default=` is client-side, applied
    # by SQLAlchemy on INSERT, so it never reaches the DDL and Alembic cannot see it;
    # only `server_default` gives the migration adding this column a value to backfill
    # the rows already in the table with, which a NOT NULL column has to have.
    gear_service_emails: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    # Which measurement system this diver reads and types in - `metric` or `imperial`
    # (`UnitSystem` in `schemas/user.py` is the vocabulary; the column is a plain
    # `VARCHAR`, the `GearItem.type` shape). Stored server-side rather than per device
    # so every client the diver signs into agrees, and nothing the API serves varies by
    # it: measurements are metric everywhere and this says who is looking (see
    # DECISIONS.md).
    #
    # Same `default`/`server_default` pair as `gear_service_emails` above, for the same
    # reason - the column is NOT NULL, so the migration adding it needs a server-side
    # default to backfill the existing rows.
    units: Mapped[str] = mapped_column(String(16), default="metric", server_default="metric")

    # Overrides `SoftDeleteMixin.is_deleted` to add an index: unlike Dive, Certification and
    # GearServiceRecord (each of which has a compound partial index whose predicate already
    # pins `is_deleted`), no other index on this table covers it. Trip and DiveSite used to
    # be on that list and are hard-deleted now, so they have no such column to cover.
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True, kw_only=True)

    @declared_attr.directive
    @classmethod
    def __table_args__(cls) -> tuple:
        return (
            # One row per stored file, the same guard `dive_file` and `certification_file`
            # carry: two rows naming one key would let either one's replacement unlink the
            # other's bytes. Nullable, and Postgres lets a unique index hold any number of
            # NULLs, so every account without a picture is unaffected.
            Index("ux_user_avatar_storage_key", "avatar_storage_key", unique=True),
            # `ix_user_email` beside it is a plain b-tree on the raw column, which no
            # `lower(email)` predicate can use - so every case-insensitive account lookup
            # would be a sequential scan of this table without this one. The invitation path
            # makes three of them (`crud.crud_invitations.account_exists_for` and the
            # operator queue's `has_account` join), and the batch invite runs the first up to
            # a hundred times in one request.
            #
            # **Not unique**, deliberately, and the distinction is the whole reason the
            # column keeps its own unique index as well. Accounts differing only in case are
            # possible today - `POST /auth/complete` inserts the onboarding token's address
            # verbatim and the Google path never lowercased its claim - so a unique
            # functional index would be a data-shape assertion this change did not make and
            # could fail to build on an existing instance. Uniqueness stays where it was; this
            # index only makes the lookup cheap.
            Index("ix_user_email_lower", func.lower(cls.email)),
        )
