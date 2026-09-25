from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, SoftDeleteMixin, TimestampMixin


class Dive(Base, PublicUUIDMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "dive"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    dive_number: Mapped[int] = mapped_column(Integer)
    # Always stored as the equivalent UTC instant, regardless of the offset the caller
    # provided it with (see `utc_offset_minutes` below) - Postgres normalizes any
    # timezone-aware value written to a `timestamptz` column to UTC internally anyway.
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration: Mapped[int] = mapped_column(Integer)
    notes: Mapped[str] = mapped_column(Text)

    # The UTC offset (in minutes, e.g. 120 for "+02:00") that `start_time` was originally
    # expressed in - the dive site's/dive computer's local time, not the viewer's. Kept
    # separately because a `timestamptz` column only stores an absolute instant and
    # can't reconstruct the original offset on its own. Combined with `start_time` to
    # reconstruct an offset-aware datetime for the API (see `split_start_time()`/
    # `combine_start_time()` in `core/utils/datetime_offset.py`) so dives always display
    # in the timezone they were actually logged in.
    #
    # **NULL is a third state, not a missing value**: the wall clock was recorded and the
    # instant is unknown (DiveJSON spec §5.2). `start_time` then holds that wall clock
    # labelled UTC, because a `timestamptz` has nowhere else to put it, and
    # `combine_start_time` hands it back naive. It exists because a converter meeting an
    # offset-less source has no honest third option. Only the logbook importer can *begin*
    # it - manual entry and the dive-computer parse path both know an offset - though a
    # `PATCH /dive/{uuid}` of such a dive's wall clock writes the NULL onward rather than
    # forcing an offset onto it. Preserve, never remove: see `core/utils/datetime_offset.py`.
    #
    # The `0` defaults survive the column becoming nullable, and deliberately: they are
    # what stops a `Dive(...)` constructed without an offset (tests, the admin panel) from
    # silently claiming the unknown state, which is a claim about the data rather than a
    # missing keyword argument. Every real write passes an explicit value; the two that
    # ever pass an explicit `None` are the importer and `patch_dive`'s preserve branch.
    utc_offset_minutes: Mapped[int | None] = mapped_column(Integer, default=0, server_default="0")
    # **A fourth state, on the same terms as the third**: the day was recorded and the time
    # of day was not (DiveJSON spec §5.2's bare `full-date`). `start_time` then holds midnight
    # of that day labelled UTC and `utc_offset_minutes` is NULL, since a day has no instant;
    # `combine_dive_start_time` reads the triple back as the bare date. Midnight rather than
    # another hour so that every sort on `start_time` places the dive at the start of its day.
    #
    # A flag rather than a nullable time column, because every query that orders or windows
    # on `start_time` keeps working untouched. `False` by default and by server default, for
    # the reason the offset keeps its `0`: a `Dive(...)` built without it cannot claim the
    # state. Only the importer begins it; `patch_dive` keeps it for a bare date sent back and
    # ends it on any date-time. See `core/utils/datetime_offset.py`.
    start_date_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    max_depth: Mapped[float | None] = mapped_column(Float, default=None)
    avg_depth: Mapped[float | None] = mapped_column(Float, default=None)
    bottom_temperature: Mapped[float | None] = mapped_column(Float, default=None)
    visibility: Mapped[int | None] = mapped_column(Integer, default=None)
    # Total ballast carried on the dive, in kilograms - a plain per-dive scalar like the
    # depths above rather than a `gear_item`, because the amount of lead isn't a piece of
    # kit the diver owns and the whole point of logging it is comparing it numerically
    # across dives (see DECISIONS.md). `Float`, not `Integer`: half-kilo increments are
    # normal, and pound-based weights don't convert to whole kilos.
    weight: Mapped[float | None] = mapped_column(Float, default=None)
    # What the water was and how high above the sea it sat, recorded as facts about the
    # dive. Both are diver knowledge, so they sit here rather than on the import-owned
    # `DiveTechScalars`; the density a *computer* was set to is `dive_recording.salinity`.
    #
    # `water_type` is a plain `VARCHAR(32)` holding a `WaterType` value
    # (`schemas/dive.py`) with no DB `CHECK` behind it, exactly like `gear_item.type` -
    # a Pydantic enum guards every write path, so a DB copy of the list would only cost a
    # `DROP`/`ADD CONSTRAINT` per new member. See DECISIONS.md.
    #
    # `altitude` is metres above sea level of the water surface, and is a different fact
    # from `dive_recording.surface_pressure_bar`: that one is a device's own barometer
    # reading, import-only and display-only, while this is the place, which a diver can type.
    # `Integer` because metre resolution is already finer than any use - computers
    # themselves bucket altitude into 300 m bands.
    water_type: Mapped[str | None] = mapped_column(String(32), default=None)
    altitude: Mapped[int | None] = mapped_column(Integer, default=None)
    trip_id: Mapped[int | None] = mapped_column(ForeignKey("trip.id", ondelete="SET NULL"), default=None, index=True)
    # The training course this dive was logged on, if any - character-for-character the
    # shape of `trip_id` above, and for the same reasons: deleting the course leaves the
    # dive with the link cleared rather than taking the dive with it.
    course_id: Mapped[int | None] = mapped_column(
        ForeignKey("course.id", ondelete="SET NULL"), default=None, index=True
    )
    # Who the diver dived with - the dive center, club or liveaboard - the same shape again.
    contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("contact.id", ondelete="SET NULL"), default=None, index=True
    )

    # Where the diver got in and where they got out, in decimal degrees. Written by the
    # import path only - a dive computer's own satellite fixes, which
    # `services/dive_parsers/positions.py` reduces to these two - so they are not on the
    # dive form either.
    #
    # Per-dive rather than on the dive site the dive links to, which is the decision this
    # reverses: a site is one pin, and an entry and an exit are two different places on a
    # drift dive, which is precisely what a fix pair records. See DECISIONS.md.
    #
    # Two `Float` columns per position, like `dive_site.latitude`/`longitude` and for the
    # same reasons; four of them here because there are two positions, not because a
    # coordinate needs four numbers.
    entry_latitude: Mapped[float | None] = mapped_column(Float, default=None)
    entry_longitude: Mapped[float | None] = mapped_column(Float, default=None)
    exit_latitude: Mapped[float | None] = mapped_column(Float, default=None)
    exit_longitude: Mapped[float | None] = mapped_column(Float, default=None)

    @declared_attr.directive
    @classmethod
    def __table_args__(cls) -> tuple:
        return (
            # Mirrors the frontend's Zod validation (`lib/validations/dive.ts`) at the DB
            # layer, so direct API calls or bugs can't insert nonsensical dive data.
            CheckConstraint("duration > 0", name="ck_dive_duration_positive"),
            CheckConstraint("visibility IS NULL OR visibility >= 0", name="ck_dive_visibility_non_negative"),
            CheckConstraint("max_depth IS NULL OR max_depth > 0", name="ck_dive_max_depth_positive"),
            CheckConstraint("avg_depth IS NULL OR avg_depth > 0", name="ck_dive_avg_depth_positive"),
            # A *pair*, unlike the two above it, and the only arithmetic relation on this
            # table: a mean cannot exceed a maximum, so a dive claiming otherwise records
            # at least one wrong number. `<=`, not `<` - a perfectly square profile is
            # unusual, not impossible. Mirrored by `validate_depth_pair` in
            # `schemas/dive.py` so the caller gets a sentence rather than an
            # `IntegrityError`; it is a *pair* rule, so the parse-side single-column
            # guards deliberately do not cover it (see
            # `test_every_single_column_bound_a_parser_can_reach_has_a_parse_side_guard`).
            CheckConstraint(
                "avg_depth IS NULL OR max_depth IS NULL OR avg_depth <= max_depth",
                name="ck_dive_avg_depth_within_max",
            ),
            # `>= 0`, unlike the depths above: diving with no lead at all is a real,
            # deliberate entry (a drysuit with a heavy undergarment, a freedive), and it's
            # worth being able to tell apart from "didn't record it" (NULL).
            CheckConstraint("weight IS NULL OR weight >= 0", name="ck_dive_weight_non_negative"),
            # Bounded on both sides because a number this far out is a unit or typo error
            # rather than an unusual dive: the Dead Sea (~-430 m) is the lowest diveable
            # surface on Earth, and the highest attested dives are the summit pool of Ojos
            # del Salado (~6 390 m). `water_type` gets no constraint of its own - see the
            # column's comment above. It also fixes `ck_dive_recording_surface_pressure_range`'s
            # floor: ambient pressure at this 6500 m ceiling is about 0.44 bar.
            CheckConstraint(
                "altitude IS NULL OR (altitude >= -450 AND altitude <= 6500)",
                name="ck_dive_altitude_range",
            ),
            # Bounded on both sides for a plain reason: these are the limits of the
            # coordinate system. A value outside them is a unit error - a FIT semicircle
            # count read as degrees, or a Suunto radian read the same way - rather than a
            # dive somewhere unusual.
            #
            # One constraint per column, mirrored one-for-one by a validator on
            # `ParsedDiveSchema`, so that every single-column bound a parser can reach
            # still has a parse-side guard (`test_every_single_column_bound_a_parser_can
            # _reach_has_a_parse_side_guard` counts them).
            CheckConstraint(
                "entry_latitude IS NULL OR (entry_latitude >= -90 AND entry_latitude <= 90)",
                name="ck_dive_entry_latitude_range",
            ),
            CheckConstraint(
                "entry_longitude IS NULL OR (entry_longitude >= -180 AND entry_longitude <= 180)",
                name="ck_dive_entry_longitude_range",
            ),
            CheckConstraint(
                "exit_latitude IS NULL OR (exit_latitude >= -90 AND exit_latitude <= 90)",
                name="ck_dive_exit_latitude_range",
            ),
            CheckConstraint(
                "exit_longitude IS NULL OR (exit_longitude >= -180 AND exit_longitude <= 180)",
                name="ck_dive_exit_longitude_range",
            ),
            # A pair, unlike every constraint above it: half a position is not a partial
            # position but a meaningless one, a dive pinned to the equator or the prime
            # meridian by whichever half survived. `dive_site` leaves the same rule to its
            # write schemas because a diver types those two numbers and has to be told
            # which one is missing; nothing types these, so the database is the right
            # place to refuse a half pair outright.
            CheckConstraint(
                "(entry_latitude IS NULL) = (entry_longitude IS NULL)",
                name="ck_dive_entry_position_pair",
            ),
            CheckConstraint(
                "(exit_latitude IS NULL) = (exit_longitude IS NULL)",
                name="ck_dive_exit_position_pair",
            ),
            # A day has no instant, so a date-only start cannot carry an offset: one would make
            # the stored midnight a claim about when the dive happened.
            CheckConstraint(
                "NOT start_date_only OR utc_offset_minutes IS NULL",
                name="ck_dive_start_date_only_has_no_offset",
            ),
            # Serves `_cached_read_dives` (`GET /dives`, by far the hottest query on this
            # table): `WHERE user_id = ... AND is_deleted = false ORDER BY start_time DESC`.
            # `is_deleted` isn't a column here - the partial predicate already pins it to
            # `false`, so Postgres can use this index for both the filter and the sort
            # without a separate sort step, while staying smaller than a 3-column index.
            # This replaces the old standalone `is_deleted` index, which was low-value as a
            # leading column (mostly `false`) and unused elsewhere on this table (every
            # other dive lookup filters by the `id` primary key instead).
            Index(
                "ix_dive_user_id_start_time",
                "user_id",
                cls.start_time.desc(),
                postgresql_where=cls.is_deleted.is_(False),
            ),
            # Serves `services.dive_stats.recalculate_dive_stats`'s
            # `COUNT`/`MAX(max_depth)`/`SUM(duration)` aggregate, run after every dive
            # create/update/delete. `max_depth`/`duration` are `INCLUDE`d (not index key
            # columns) purely so Postgres can answer the aggregate as an index-only scan
            # instead of a heap fetch per matching dive - they aren't used for filtering or
            # ordering, so they don't need to be part of the index's sort key.
            Index(
                "ix_dive_user_id_stats",
                "user_id",
                postgresql_where=cls.is_deleted.is_(False),
                postgresql_include=["max_depth", "duration"],
            ),
        )
