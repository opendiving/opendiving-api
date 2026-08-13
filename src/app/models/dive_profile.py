from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, deferred, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, TimestampMixin


class DiveProfile(Base, PublicUUIDMixin, TimestampMixin):
    """A dive's per-sample depth / temperature / tank-pressure curves.

    Derived from the dive's stored export (`DiveFile`), never uploaded: the samples are
    extracted server-side by `DiveParser.parse_profile` during `PUT /dive/{uuid}/file`,
    which is the only place that has both the bytes and the parse token proving where
    they came from. Nothing here is client-supplied, which is the whole reason
    `/dive/parse` doesn't return a profile - see `services/dive_profiles.py`.

    One row per dive, with each channel's series in a JSONB `data` payload rather than a
    row per sample: several hundred (Suunto) to several thousand (Ocean, 1 Hz) readings
    per dive, only ever fetched whole for one dive and drawn. A sample table would exist
    purely to be `ORDER BY t`-ed back into the arrays below.

    `data` holds independently-sampled per-channel series, not one shared time axis, plus
    the moments the device marked rather than sampled:

        {"depth":       {"t": [0, 10, 20], "v": [139, 372, 632]},
         "ceiling":     {"t": [20],        "v": [300]},
         "temperature": {"t": [0, 1, 2],   "v": [219, 219, 218]},
         "pressure":    [{"gas_number": 1, "t": [0, 10], "v": [2052, 2041]}],
         "events":      [{"t": 0, "type": "gas_switch", "gas_number": 1}]}

    `t` is integer elapsed seconds from the first sample; `v` is integer-scaled (depth and
    ceiling in cm, temperature in 0.1 C, pressure in 0.1 bar) so a float round-trip can't
    reintroduce `20.600000000000023`-class noise several thousand times per dive. There
    are no nulls inside a series - a sensor dropout is a gap in `t`, which the chart breaks
    the polyline across, and on the ceiling channel a gap is a stretch of the dive with no
    decompression obligation. See `services/dive_profiles.py` for the shape's full
    rationale.

    Mirrors `DiveFile` deliberately: a `deferred` payload, one unique index on `dive_id`,
    and no `SoftDeleteMixin` (a soft-deleted blob occupies its bytes forever with nothing
    able to read it). No `user_id` either - unlike `dive_file` there is no per-user
    uniqueness to enforce in this table, and every read resolves through
    `_get_owned_dive`.

    **The `ON DELETE CASCADE` below never fires.** Dive deletion is application-level
    (`is_deleted`), so no `DELETE FROM dive` ever runs - the same trap `delete_files_for_dive`
    and `soft_delete_schedules_for_gear_item` exist to work around. `delete_profile_for_dive`
    in `services/dive_profiles.py` is what actually removes these rows, and it is called
    from both the file-delete and the dive-delete paths.
    """

    __tablename__ = "dive_profile"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, unique=True, primary_key=True, init=False)
    dive_id: Mapped[int] = mapped_column(ForeignKey("dive.id", ondelete="CASCADE"))

    # Idempotency key for extraction. `source_sha256` is the `dive_file.sha256` these
    # samples came out of; together with `extractor_version` it is the whole test for
    # "is this profile still current" (`should_extract`), and the pair is also the ETag
    # the read endpoint serves.
    source_sha256: Mapped[str] = mapped_column(String(64))
    # `DiveParser.key` of the parser that produced these samples, here for the same
    # reason it is on `dive_file`: a backfill selects the subset it knows how to re-read.
    parser_key: Mapped[str] = mapped_column(String(32))
    extractor_version: Mapped[int] = mapped_column(Integer)

    # Summary, deliberately *not* inside `data`: this is what answers "does this dive
    # have a profile, and which curves would a chart draw" for the dive detail response,
    # without decoding tens of KB of JSONB. Stored in the same integer scales as the
    # series (`_c10` is 0.1 C, `_bar10` is 0.1 bar) and converted to display units on the
    # way out.
    duration_seconds: Mapped[int] = mapped_column(Integer)
    depth_sample_count: Mapped[int] = mapped_column(Integer)

    # `deferred` so any query against this table returns the summary only unless the
    # series are asked for explicitly with `undefer` - which only `load_profile` does.
    #
    # `nullable=False` is spelled out because wrapping the column in `deferred()` hides
    # the `Mapped[dict]` annotation from SQLAlchemy's nullability inference, which would
    # otherwise emit a nullable column - the same trap already documented on
    # `DiveFile.data`.
    #
    # Declared here, in the middle of the summary block it isn't part of, because these
    # models are dataclasses: a column with no default cannot follow one that has a
    # default, and the extremes below are all nullable.
    data: Mapped[dict] = deferred(mapped_column(JSONB, nullable=False))

    # Nullable because a channel a file doesn't carry has no extremes - which is also
    # what `channels` on the read schema is derived from, so there is no redundant
    # "which curves are present" column to drift out of step with the payload.
    max_depth_cm: Mapped[int | None] = mapped_column(Integer, default=None)
    # In the same centimeters as `max_depth_cm`, since a ceiling is a depth and is drawn
    # against the depth axis. NULL means the dive never had a decompression obligation,
    # which is the same test the `ceiling` channel's presence is derived from.
    max_ceiling_cm: Mapped[int | None] = mapped_column(Integer, default=None)
    min_temperature_c10: Mapped[int | None] = mapped_column(Integer, default=None)
    max_temperature_c10: Mapped[int | None] = mapped_column(Integer, default=None)
    min_pressure_bar10: Mapped[int | None] = mapped_column(Integer, default=None)
    max_pressure_bar10: Mapped[int | None] = mapped_column(Integer, default=None)
    # Not an extreme like the columns above, and it is here for the reason they are: to
    # answer "is there anything to draw" for the dive detail response without decoding the
    # payload. Events have no extremes to be derived from, so this is a plain count - `0`
    # is a profile whose file recorded no events, and NULL is one extracted before this
    # extractor version recorded any.
    event_count: Mapped[int | None] = mapped_column(Integer, default=None)

    __table_args__ = (
        # One profile per dive. Re-importing a different export for the same dive
        # replaces it, exactly as it replaces the `dive_file` row it was derived from.
        Index("ux_dive_profile_dive_id", "dive_id", unique=True),
    )
