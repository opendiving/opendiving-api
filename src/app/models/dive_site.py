from sqlalchemy import Float, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, TimestampMixin


class DiveSite(Base, PublicUUIDMixin, TimestampMixin):
    __tablename__ = "dive_site"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    notes: Mapped[str] = mapped_column(Text, default="")
    # Two plain floats rather than PostGIS: a dive site is a point, and the only questions
    # asked of it are "show it" and "list them" - see DECISIONS.md.
    latitude: Mapped[float | None] = mapped_column(Float, default=None)
    longitude: Mapped[float | None] = mapped_column(Float, default=None)
    # The locality, flat and prefixed: the same place object a trip part carries
    # (`schemas/location.py`). The prefix is what keeps this row's two positions apart -
    # `latitude`/`longitude` is the pin a diver dropped on the site, `location_latitude`/
    # `location_longitude` the centre of the town the geocoder resolved, and a reader must
    # not take the second for the first. `location_name` is what says whether there is a
    # locality at all.
    location_name: Mapped[str | None] = mapped_column(String(255), default=None)
    location_full_name: Mapped[str | None] = mapped_column(String(512), default=None)
    location_latitude: Mapped[float | None] = mapped_column(Float, default=None)
    location_longitude: Mapped[float | None] = mapped_column(Float, default=None)
    location_bbox_south: Mapped[float | None] = mapped_column(Float, default=None)
    location_bbox_north: Mapped[float | None] = mapped_column(Float, default=None)
    location_bbox_west: Mapped[float | None] = mapped_column(Float, default=None)
    location_bbox_east: Mapped[float | None] = mapped_column(Float, default=None)

    @declared_attr.directive
    @classmethod
    def __table_args__(cls) -> tuple:
        return (
            # Case-insensitive uniqueness per user on (name, locality name). COALESCE maps
            # a NULL locality to '' so two sites with the same name and no locality are
            # also considered duplicates. The key is the locality's *name* and none of its
            # other members: a place is identified by what it is called, and two sites in
            # "Dahab, Egypt" collide whether or not a geocoder filled in a centre for one
            # of them. Reusing a deleted site's pair needs no exemption here: the row is
            # gone, so it constrains nothing.
            Index(
                "ux_dive_site_user_id_name_location_lower",
                "user_id",
                func.lower(cls.name),
                func.coalesce(func.lower(cls.location_name), ""),
                unique=True,
            ),
            # Serves `read_dive_sites` (`GET /dive-sites`): `WHERE user_id = ... ORDER BY
            # name ASC`. Distinct from the unique index above, which is keyed on
            # `lower(name)` and can't satisfy a plain (case-sensitive) `ORDER BY name`.
            # Replaces the old standalone `is_deleted` index, which was low-value as a
            # leading column and unused elsewhere on this table (every other dive site
            # lookup filters by the `id` primary key instead).
            Index(
                "ix_dive_site_user_id_name",
                "user_id",
                "name",
            ),
        )
