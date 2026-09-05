from sqlalchemy import JSON, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, TimestampMixin


class DiveFormPreset(Base, PublicUUIDMixin, TimestampMixin):
    """A named set of dive-form fields this diver keeps hidden, e.g. "Basic",
    "Recreational" or "Warm water".

    A preset is a **snapshot, not a live binding**: applying one copies its `hidden_fields`
    into `user.dive_form_hidden_fields`, and toggling a field afterwards moves that column
    and not this row, until the diver writes the current state back onto the preset. So a
    one-off "show me altitude just this once" never edits a preset, and a diver who has
    deleted every preset still has somewhere to toggle. Nothing else in the app reads these
    rows - they are a shortcut for filling in one column, the same relationship `GearSet`
    has to a dive's gear list.

    Every account is seeded with three of these at registration (see
    `services/dive_form_presets.py`), as ordinary rows rather than as anything the code
    special-cases: each is editable, renamable and deletable like one the diver wrote, and
    the restore action adds back whichever of the three no current preset carries by name.
    """

    __tablename__ = "dive_form_preset"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    # The keys hidden by this preset, as `DiveFormField` values in that enum's declaration
    # order with duplicates collapsed - `schemas/dive_form_preset.py` canonicalizes every
    # write, so two equal sets are two equal lists here.
    #
    # `JSON` rather than `ARRAY`: `webauthn_credential.transports` is the precedent for a
    # `list[str]` column in this schema and `ARRAY` has none. Nothing queries into it -
    # the list is read and written whole - so the indexable-element argument for `ARRAY`
    # or `JSONB` buys nothing.
    #
    # **What is stored is the hidden set, never the visible set.** A field the form gains
    # later is visible under every existing preset until somebody hides it, which is the
    # right default for a new optional input - and it is what makes "Technical" the empty
    # list rather than a list that goes stale.
    #
    # `default_factory` rather than `default=[]`, because a bare mutable default would be
    # shared by every instance; `server_default` is what backfills the DDL, the same
    # `default`/`server_default` pairing `user.gear_service_emails` documents.
    hidden_fields: Mapped[list[str]] = mapped_column(JSON, default_factory=list, server_default="[]")

    @declared_attr.directive
    @classmethod
    def __table_args__(cls) -> tuple:
        return (
            # Case-insensitive uniqueness per user on name - same pattern as
            # `gear_set`/`trip`/`dive_site` (see DECISIONS.md), and what "restore adds back
            # whichever default is missing *by name*" is decided against.
            Index(
                "ux_dive_form_preset_user_id_name_lower",
                "user_id",
                func.lower(cls.name),
                unique=True,
            ),
            # Serves `read_dive_form_presets` (`GET /dive-form-presets`):
            # `WHERE user_id = ... ORDER BY name ASC`.
            Index(
                "ix_dive_form_preset_user_id_name",
                "user_id",
                "name",
            ),
        )
