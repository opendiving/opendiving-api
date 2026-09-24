from datetime import date
from typing import cast

from sqlalchemy import JSON, Boolean, Column, Date, Index, String, Table, func
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

    # Which dive-form fields this diver currently keeps hidden - `DiveFormField` values in
    # `schemas/dive_form_preset.py`, canonicalized on every write. The account's *current*
    # state, not a preset: applying a preset copies that preset's set in here, and toggling
    # a single field afterwards moves this column alone.
    #
    # Server-side rather than per device, unlike the per-field entry-unit switch, so the
    # form a diver arranged follows them across devices and the first paint already has the
    # right fields - the list rides along on `get_current_user`, which selects every mapped
    # column, so nothing has to be loaded before the form knows what to show.
    #
    # `[]` on a fresh account: a new diver sees the form exactly as it was before presets
    # existed, and the three seeded presets are one click away.
    #
    # `JSON` for the same reason `dive_form_preset.hidden_fields` is (see there), and the
    # same `default_factory`/`server_default` pairing as the two columns above - `default=`
    # is client-side and invisible to Alembic, so only `server_default` gives the migration
    # adding this NOT NULL column a value for the rows already in the table.
    dive_form_hidden_fields: Mapped[list[str]] = mapped_column(JSON, default_factory=list, server_default="[]")

    # What a dive shop's desk asks for, held once so a diver stops writing it out on
    # arrival. Columns on the account rather than a diver-owned table: one value each, no
    # metadata and no history worth keeping, and `get_current_user` already selects every
    # mapped column. The two pictures are the opposite case - each keeps an original with a
    # name, a type and a crop - which is why they have `user_picture`.
    #
    # Every one is nullable, so none carries a `server_default`: that pair exists for a
    # `NOT NULL` column being added over rows that already exist (see `gear_service_emails`
    # above), and "not filled in" is the ordinary state for all of these. Clearing one is an
    # explicit `null` on `PATCH /user`.
    #
    # The phone numbers are free text bounded by length - shops in six countries write them
    # six ways, and nothing here dials one. Every width is DiveJSON §6.1's bound on the member
    # the column travels as, so an import can write whatever a conforming document carries.
    date_of_birth: Mapped[date | None] = mapped_column(Date, default=None)
    phone: Mapped[str | None] = mapped_column(String(32), default=None)
    emergency_contact_name: Mapped[str | None] = mapped_column(String(255), default=None)
    emergency_contact_phone: Mapped[str | None] = mapped_column(String(32), default=None)
    emergency_contact_relationship: Mapped[str | None] = mapped_column(String(64), default=None)
    insurance_provider: Mapped[str | None] = mapped_column(String(255), default=None)
    insurance_policy_number: Mapped[str | None] = mapped_column(String(64), default=None)
    insurance_expires_on: Mapped[date | None] = mapped_column(Date, default=None)

    # Overrides `SoftDeleteMixin.is_deleted` to add an index: unlike Dive, Certification and
    # GearServiceRecord (each of which has a compound partial index whose predicate already
    # pins `is_deleted`), no other index on this table covers it. Trip and DiveSite used to
    # be on that list and are hard-deleted now, so they have no such column to cover.
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True, kw_only=True)

    __mapper_args__ = {"exclude_properties": ["avatar_storage_key", "avatar_sha256"]}

    @declared_attr.directive
    @classmethod
    def __table_args__(cls) -> tuple:
        return (
            # The avatar's rendition, as it was stored before `user_picture` held it. On the
            # table and off the mapper (`__mapper_args__`): the build before this one selects
            # every column it maps on every signed-in request, so the columns stay until that
            # build has stopped serving, and no request of this one selects them. Written
            # beside the avatar's row on every change so the two agree, and read by the purge
            # and the sweeper alone.
            Column("avatar_storage_key", String(255), nullable=True),
            Column("avatar_sha256", String(64), nullable=True),
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


# The avatar's two columns, for the statements that still write and read them.
user_table = cast(Table, User.__table__)
USER_AVATAR_STORAGE_KEY = user_table.c.avatar_storage_key
USER_AVATAR_SHA256 = user_table.c.avatar_sha256
