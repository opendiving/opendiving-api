from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, TimestampMixin


class Contact(Base, PublicUUIDMixin, TimestampMixin):
    """A party a diver deals with - a dive center, a school, a shop, a place they stayed, a
    club, a friend's house - picked from a list rather than typed again.

    Referenced by a dive (who it was dived with), a course and a certification (who ran the
    course), a gear service record (who did the work) and a trip part (where the diver
    slept). What the party *is* lives in `roles`, a set, so one row can be a resort's dive
    center and its rooms at once.

    Owned by one diver, like a dive site and unlike a species: the name, the phone and the
    notes are what this diver recorded, and two divers' "Blue Ocean" need not be one place.

    No `SoftDeleteMixin`, following trips, sites, gear and courses: deleting a contact
    removes the row, and the `ON DELETE SET NULL` on all five references unlinks it from
    everything that named it. See *"The row goes, and so does everything pointing at it"*
    in DECISIONS.md.
    """

    __tablename__ = "contact"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    # `ContactRole` values (`schemas/contact.py`) in that enum's declaration order with
    # duplicates collapsed, possibly none. See *"Contact roles are a JSON list, in vocabulary
    # order"* in DECISIONS.md.
    roles: Mapped[list[str]] = mapped_column(JSON, default_factory=list, server_default="[]")
    phone: Mapped[str | None] = mapped_column(String(32), default=None)
    email: Mapped[str | None] = mapped_column(String(255), default=None)
    website: Mapped[str | None] = mapped_column(String(512), default=None)
    # The postal address, flat and prefixed as a dive site's locality is under `location_`;
    # the wire nests it as one `address` object. `address_country` is what says whether
    # there is an address at all - `ck_contact_address_has_country` below.
    address_street: Mapped[str | None] = mapped_column(String(255), default=None)
    address_city: Mapped[str | None] = mapped_column(String(255), default=None)
    address_postcode: Mapped[str | None] = mapped_column(String(32), default=None)
    address_region: Mapped[str | None] = mapped_column(String(255), default=None)
    address_country: Mapped[str | None] = mapped_column(String(255), default=None)
    notes: Mapped[str] = mapped_column(Text, default="")

    @declared_attr.directive
    @classmethod
    def __table_args__(cls) -> tuple:
        return (
            # An address is anchored on its country (DiveJSON §6.19 and UDDF's `<address>`
            # both require one), so a row with a street and no country is an address no
            # export can write. The request schema requires the country already; this is
            # the backstop for the admin panel, which writes the columns flat.
            CheckConstraint(
                "address_country IS NOT NULL OR (address_street IS NULL AND address_city IS NULL "
                "AND address_postcode IS NULL AND address_region IS NULL)",
                name="ck_contact_address_has_country",
            ),
            # Case-insensitive uniqueness per user on name - see *"Case-insensitive per-user
            # uniqueness"* in DECISIONS.md. A second "Blue Ocean" is nearly always the first
            # typed twice; two branches of one operator differ in name anyway.
            Index(
                "ux_contact_user_id_name_lower",
                "user_id",
                func.lower(cls.name),
                unique=True,
            ),
            # Serves `GET /contacts`: `WHERE user_id = ... ORDER BY name ASC`, which the
            # `lower(name)`-keyed index above cannot satisfy.
            Index("ix_contact_user_id_name", "user_id", "name"),
        )
