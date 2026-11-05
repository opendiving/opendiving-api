from datetime import date, datetime

from sqlalchemy import JSON, CheckConstraint, Date, DateTime, Float, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, TimestampMixin


class DiveSite(Base, PublicUUIDMixin, TimestampMixin):
    __tablename__ = "dive_site"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    # Who owns the row: an account, whose private site only it can read or change, or, where
    # null, OpenDiving Commons, whose record every account can read and no ordinary request
    # can change. A change of owner is what makes a diver's site a Commons record.
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    # Two plain floats rather than PostGIS: a dive site is a point, and the only questions
    # asked of it are "show it", "list them" and which Commons records lie near it, which a
    # range over the two columns answers - see DECISIONS.md.
    latitude: Mapped[float | None] = mapped_column(Float, default=None)
    longitude: Mapped[float | None] = mapped_column(Float, default=None)
    # The locality, flat and prefixed: the same place object a trip part carries
    # (`schemas/location.py`). The prefix is what keeps this row's two positions apart -
    # `latitude`/`longitude` is the pin a diver dropped on the site, `location_latitude`/
    # `location_longitude` the centre of the town the geocoder resolved, and a reader must
    # not take the second for the first. `location_name` is what says whether there is a
    # locality at all.
    location_name: Mapped[str | None] = mapped_column(String(255), default=None)
    location_latitude: Mapped[float | None] = mapped_column(Float, default=None)
    location_longitude: Mapped[float | None] = mapped_column(Float, default=None)
    location_bbox_south: Mapped[float | None] = mapped_column(Float, default=None)
    location_bbox_north: Mapped[float | None] = mapped_column(Float, default=None)
    location_bbox_west: Mapped[float | None] = mapped_column(Float, default=None)
    location_bbox_east: Mapped[float | None] = mapped_column(Float, default=None)
    # What the place is, as against what a dive there recorded: a site's water type,
    # altitude and entry types are the place's, a dive's are the day's, and neither is filled
    # from the other. The lists are JSON on `contact.roles`' terms - nothing queries into
    # them but the other-names search, which reads the elements as text - and every writer
    # stores them conforming (`schemas/dive_site.py`): no other name equal to `name` or to
    # another once trimmed and case-folded, no registry entry twice, `entry_types` in
    # `EntryType`'s order. `water_type` and `entry_types` carry no `CHECK`, as the dive's
    # vocabularies carry none; the depths and the altitude carry the format's bounds.
    other_names: Mapped[list[str]] = mapped_column(JSON, default_factory=list, server_default="[]")
    # `{"registry", "identifier"}` objects, DiveJSON's External Id without its extensions.
    external_ids: Mapped[list[dict[str, str]]] = mapped_column(JSON, default_factory=list, server_default="[]")
    depth_from: Mapped[float | None] = mapped_column(Float, default=None)
    depth_to: Mapped[float | None] = mapped_column(Float, default=None)
    water_type: Mapped[str | None] = mapped_column(String(32), default=None)
    altitude: Mapped[int | None] = mapped_column(Integer, default=None)
    entry_types: Mapped[list[str]] = mapped_column(JSON, default_factory=list, server_default="[]")
    # A Commons record's provenance, null on a private site: where it came from and on what
    # terms, and for a seed the registry entry it was seeded from and the day it was retrieved.
    # That entry is what places the seed's file in the repository, so it is held apart from
    # `external_ids`, which a reviewer can edit.
    commons_source: Mapped[str | None] = mapped_column(String(32), default=None)
    commons_license: Mapped[str | None] = mapped_column(String(32), default=None)
    commons_seed_registry: Mapped[str | None] = mapped_column(String(255), default=None)
    commons_seed_identifier: Mapped[str | None] = mapped_column(String(255), default=None)
    commons_retrieved: Mapped[date | None] = mapped_column(Date, default=None)
    # When a review last wrote this row, and the mark the sync leaves alone whatever
    # `COMMONS_PRIMARY` says: a primary that restarts without its setting must not hand an
    # accepted change back to the bundle. On the row rather than read off the proposals, which
    # an account's deletion takes. This instance's own, so nothing publishes it.
    commons_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

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
            # gone, so it constrains nothing. A Commons record's null owner compares unequal
            # to every other, so it binds nothing either: a name is not unique across the world.
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
            CheckConstraint("depth_from IS NULL OR depth_from >= 0", name="ck_dive_site_depth_from_non_negative"),
            CheckConstraint("depth_to IS NULL OR depth_to >= 0", name="ck_dive_site_depth_to_non_negative"),
            CheckConstraint(
                "depth_from IS NULL OR depth_to IS NULL OR depth_from <= depth_to", name="ck_dive_site_depth_range"
            ),
            # The dive's range, `ck_dive_altitude_range`, for the same water.
            CheckConstraint(
                "altitude IS NULL OR (altitude >= -450 AND altitude <= 6500)", name="ck_dive_site_altitude_range"
            ),
            # Neither owner can be half-written: a private row carries no provenance, a Commons
            # row its source and licence, and a seed its entry and date where a contribution
            # has neither.
            CheckConstraint(
                "(user_id IS NOT NULL AND commons_source IS NULL AND commons_license IS NULL "
                "AND commons_seed_registry IS NULL AND commons_seed_identifier IS NULL "
                "AND commons_retrieved IS NULL) "
                "OR (user_id IS NULL AND commons_license IS NOT NULL AND ("
                "(commons_source IN ('openstreetmap', 'wikidata') AND commons_seed_registry IS NOT NULL "
                "AND commons_seed_identifier IS NOT NULL AND commons_retrieved IS NOT NULL) "
                "OR (commons_source = 'contribution' AND commons_seed_registry IS NULL "
                "AND commons_seed_identifier IS NULL AND commons_retrieved IS NULL)))",
                name="ck_dive_site_owner_provenance",
            ),
        )
