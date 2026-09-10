from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, deferred, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, TimestampMixin


class DiveProfile(Base, PublicUUIDMixin, TimestampMixin):
    """One recording's per-sample depth / temperature / tank-pressure curves.

    Derived from the recording's stored exports (`DiveFile`) on every path but one: the
    samples are extracted server-side by `DiveParser.parse_profile` during
    `POST /dive/{uuid}/recordings`, which is the only place that has both the bytes and the
    parse token proving where they came from. That is still the whole reason `/dive/parse`
    doesn't return a profile - see `services/dive_profiles.py`.

    **One row per recording, not per dive.** A diver on two computers has two profiles of
    one dive, drawn from two devices' samples, and neither is a version of the other. The
    `t` axis is elapsed seconds from the *recording's* start, which is why the recording
    carries a `start_time` of its own: a second computer that entered the water 223 seconds
    later has a profile whose zero is 223 seconds after the dive's.

    **The exception is logbook import**, which does write samples the client supplied. It
    is the one sanctioned path, on the terms `DECISIONS.md` records under *"Importing a
    logbook is the one client-supplied profile"*: the samples land in the importer's own
    logbook and nowhere else, through a two-phase preview/apply, with every channel
    re-validated and re-normalized before it is stored. Deliberately not "the caller's own
    backup" - that route converts a UDDF file or a `.ssrf` on the way in, so the document
    may have been written by another application entirely, and the argument is about whose
    logbook it lands in rather than about who wrote the file. The provenance columns below
    say which path a row came from - `parser_key` is `divejson_import` on a row that
    arrived that way, whatever the upload was before it was converted.

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

    Mirrors `DiveFile` deliberately: a `deferred` payload and no `SoftDeleteMixin` (a
    soft-deleted blob occupies its bytes forever with nothing able to read it). No
    `user_id` either - unlike `dive_file` there is no per-user uniqueness to enforce in
    this table, and every read resolves through `_get_owned_dive`.

    **The `ON DELETE CASCADE` to `dive` never fires.** Dive deletion is application-level
    (`is_deleted`), so no `DELETE FROM dive` ever runs - the same trap `delete_files_for_dive`
    and `delete_files_for_certification` exist to work around, and the one the gear tables
    got out of by going hard-delete. `delete_profile_for_dive` in `services/dive_profiles.py`
    is what actually removes these rows on that path.

    The cascade to `dive_recording` **does** fire, because recordings are hard-deleted:
    removing a recording takes its profile and its files with it in the database rather
    than in application code. The blobs still need `delete_after_commit`, which is why
    `services/dive_recordings.py` collects the storage keys before issuing the delete.
    """

    __tablename__ = "dive_profile"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    # The recording these samples are of. Unique below, so a recording has at most one
    # profile - which is what "a recording is one device's record" means, and what the
    # fill rule depends on: a second file of one recording contributes the *channels* the
    # first did not carry, into this one row, and never a second row beside it.
    recording_id: Mapped[int] = mapped_column(ForeignKey("dive_recording.id", ondelete="CASCADE"))
    # Denormalized off the recording, exactly as on `dive_file`: `erase_dive` and the
    # export loader both want "this dive's profiles" without joining.
    dive_id: Mapped[int] = mapped_column(ForeignKey("dive.id", ondelete="CASCADE"), index=True)

    # Idempotency key for extraction. `source_sha256` is what these samples came out of;
    # together with `extractor_version` it is the whole test for "is this profile still
    # current" (`should_extract`), and the pair is also the ETag the read endpoint serves.
    # For a recording holding one file it is that file's own `sha256`, exactly as before;
    # for one holding several it is the SHA-256 over their digests concatenated in attach
    # order, so a second file arriving invalidates the profile the first produced.
    source_sha256: Mapped[str] = mapped_column(String(64))
    # **Which of three things this profile is**, which is more than "which parser read it":
    # a `DiveParser.key` means the samples were extracted from the recording's files in
    # order and can be extracted again; `divejson_import` means a document supplied them and
    # no file here can re-yield them; `merge` means two recordings' samples were folded on
    # one axis. The two non-parser values are what both backfills refuse to overwrite, and
    # the distinction is on the *profile* rather than on a file precisely because a merged
    # recording may still hold the files either part had.
    parser_key: Mapped[str] = mapped_column(String(32))
    extractor_version: Mapped[int] = mapped_column(Integer)

    # Summary, deliberately *not* inside `data`: this is what answers "does this dive
    # have a profile, and which curves would a chart draw" for the dive detail response,
    # without decoding tens of KB of JSONB. Stored in the same integer scales as the
    # series (`_c10` is 0.1 C, `_bar10` is 0.1 bar) and converted to display units on the
    # way out.
    # Seconds, and named for the wire member it feeds - `profile.duration` (DiveJSON spec
    # §6.4) - rather than carrying a unit suffix of its own, so storage and the published
    # format speak one word. Not the dive's own `duration`, which is a column on another
    # table and a different quantity: the diver's logged length, which may have been
    # hand-edited.
    duration: Mapped[int] = mapped_column(Integer)
    depth_sample_count: Mapped[int] = mapped_column(Integer)

    # `deferred` so any query against this table returns the summary only unless the
    # series are asked for explicitly with `undefer` - which only `load_profile` does.
    #
    # `nullable=False` is spelled out because wrapping the column in `deferred()` hides
    # the `Mapped[dict]` annotation from SQLAlchemy's nullability inference, which would
    # otherwise emit a nullable column. `DiveFile.data` and `CertificationFile.data` used
    # to carry the same note; they are gone (their payloads moved to the files volume), so
    # this is the last column in the app the trap applies to.
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
    # Which gas was breathed for how long and how deep, one entry per gas number - the one
    # thing a multi-tank dive needs that no other table records. Here rather than inside
    # `data` because a dive read has to join it against the mixtures on every detail
    # response, and `data` is `deferred` precisely so that never loads tens of KB.
    #
    # NULL is "extracted before attribution existed" and `[]` is "this extractor looked and
    # found nothing to attribute" - the same distinction `event_count` draws, and the same
    # one a later backfill selects on.
    gas_attribution: Mapped[list | None] = mapped_column(JSONB, default=None)

    __table_args__ = (
        # One profile per recording. `ux_dive_profile_dive_id` - one per *dive* - is gone:
        # two computers on one dive are two recordings and two profiles, and the app's own
        # UDDF export takes the primary one rather than pretending there is only ever one.
        Index("ux_dive_profile_recording_id", "recording_id", unique=True),
    )
