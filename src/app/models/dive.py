from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text
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
    # What the water was and how high above the sea it sat - the two calibration settings
    # a dive computer carries, recorded here as facts about the dive. Both are diver
    # knowledge first (the FIT import only ever *seeds* `water_type` through the form's
    # prefill), so they sit here rather than on the import-owned `DiveTechScalars`.
    #
    # `water_type` is a plain `VARCHAR(32)` holding a `WaterType` value
    # (`schemas/dive.py`) with no DB `CHECK` behind it, exactly like `gear_item.type` -
    # a Pydantic enum guards every write path, so a DB copy of the list would only cost a
    # `DROP`/`ADD CONSTRAINT` per new member. See DECISIONS.md.
    #
    # `altitude` is metres above sea level of the water surface, and is a different fact
    # from `surface_pressure_bar` below: that one is the device's own barometer reading,
    # import-only and display-only, while this is the place, which a diver can type.
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

    # Oxygen-exposure and surface-pressure readings, written **only** by the import path
    # (`services/dive_files.py::store_dive_file`) and never through the dive form. They
    # are the dive computer's own accounting - CNS and OTU depend on the algorithm the
    # device ran and on the diver's exposure history, neither of which is reconstructable
    # from a logged dive - so a hand-typed value would be a guess wearing a reading's
    # clothes. See DECISIONS.md.
    #
    # Start *and* end for both, because the pair is what a diver reads: CNS 8 -> 9 on a
    # repetitive dive says something an end value of 9 alone does not.
    #
    # `Float`, not `Integer`, for CNS: the Suunto DM5 XML export rounds it to whole
    # percent but the JSON export of the same dive records 0.069 (a 0-1 fraction, i.e.
    # 6.9 %), and storing the finer reading as 7 would throw away precision the file has.
    cns_start: Mapped[float | None] = mapped_column(Float, default=None)
    cns_end: Mapped[float | None] = mapped_column(Float, default=None)
    otu_start: Mapped[float | None] = mapped_column(Float, default=None)
    otu_end: Mapped[float | None] = mapped_column(Float, default=None)
    # Ambient pressure at the surface, in bar - altitude and weather. Display-only:
    # `services/dive_gas.py` deliberately assumes 1 bar at the surface, and that is a
    # recorded choice rather than an oversight, so this column does not feed SAC/RMV.
    surface_pressure_bar: Mapped[float | None] = mapped_column(Float, default=None)

    # Where the diver got in and where they got out, in decimal degrees. Written by the
    # same import path as the readings above and for the same reason - a dive computer's
    # own satellite fixes, which `services/dive_parsers/positions.py` reduces to these
    # two - so they are not on the dive form either.
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
            # column's comment above.
            CheckConstraint(
                "altitude IS NULL OR (altitude >= -450 AND altitude <= 6500)",
                name="ck_dive_altitude_range",
            ),
            # `>= 0` rather than `> 0` for the same reason as `weight`: a dive that began
            # with no oxygen loading at all records a real 0, and that is worth telling
            # apart from "didn't record it". No upper bound - CNS above 100 % is exactly
            # the reading a diver most needs to see, and clamping it would hide it.
            CheckConstraint("cns_start IS NULL OR cns_start >= 0", name="ck_dive_cns_start_non_negative"),
            CheckConstraint("cns_end IS NULL OR cns_end >= 0", name="ck_dive_cns_end_non_negative"),
            CheckConstraint("otu_start IS NULL OR otu_start >= 0", name="ck_dive_otu_start_non_negative"),
            CheckConstraint("otu_end IS NULL OR otu_end >= 0", name="ck_dive_otu_end_non_negative"),
            # Bounded on both sides, unlike everything above, because this one has real
            # physical limits and the corpus sits well inside them (1.031-1.067 bar across
            # 384 exports). Anything outside the band is a unit error - both Suunto exports
            # write this field in Pascal, where an unconverted 105 700 is off by five orders
            # of magnitude - rather than a dive somewhere unusual.
            #
            # The floor is 0.4, not 0.5, and it is `ck_dive_altitude_range` that fixes it:
            # ambient pressure at this table's own 6500 m altitude ceiling is about 0.44
            # bar, so a 0.5 floor refused readings the altitude bound blesses. It is the
            # DiveJSON floor too (spec §6.2), which is where the contradiction was noticed -
            # an importer must not drop a value the format admits. Mirrored by
            # `_drop_implausible_surface_pressure` in `schemas/parsed_dive.py`.
            CheckConstraint(
                "surface_pressure_bar IS NULL OR (surface_pressure_bar >= 0.4 AND surface_pressure_bar <= 1.2)",
                name="ck_dive_surface_pressure_range",
            ),
            # Bounded on both sides like the surface pressure, and for a plainer reason:
            # these are the limits of the coordinate system. A value outside them is a
            # unit error - a FIT semicircle count read as degrees, or a Suunto radian
            # read the same way - rather than a dive somewhere unusual.
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
