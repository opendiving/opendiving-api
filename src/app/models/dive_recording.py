from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, TimestampMixin


class DiveRecording(Base, PublicUUIDMixin, TimestampMixin):
    """One device's record of one dive.

    A dive used to have at most one stored export and at most one profile, both keyed on
    `dive_id` by a unique index. That model has three things it cannot express, and each
    of them is an ordinary thing a diver does: wearing two computers, exporting the same
    computer twice in two formats, and having a dive cut in half by a computer that shut
    down mid-water. A recording is the row that makes all three representable - the
    plural unit between the dive and the files, matching DiveJSON's own §6.4a.

    **A recording need not have a file.** Logbook import stores no bytes for a converted or
    a bare document (see *"A bare document creates no file rows"* in `DECISIONS.md`), so a
    recording it creates carries a device, a start and a profile and nothing else. That is
    first-class rather than degenerate: it is how every UDDF and `.ssrf` dive in the app
    arrives, and both backfills are written to leave it alone.

    **Ordinal 0 is primary**, which is order rather than a flag - the same choice the
    format makes, and for the format's reason: a flag every writer has to set is a value
    every reader has to default. The primary recording is the one a single-profile
    consumer takes (the app's own UDDF export does exactly this), and it is the only one
    whose file writes the dive's tech scalars.

    **`user_id` is denormalized off `dive` and the match query is why.** Every gate in
    `services/dive_recordings.py` runs per account over `(user_id, start_time)`, comparing
    an incoming recording against candidates from the whole logbook rather than from one
    dive - so the index it needs cannot be reached through a join without dragging `dive`
    into a query that wants nothing else from it. `dive_file` denormalizes the same column
    for the same shape of reason.

    No `SoftDeleteMixin`, matching `dive_file` and `dive_profile` below it: a soft-deleted
    recording would hold its files' bytes on the volume with nothing able to read them.
    Deletion is `services/dive_recordings.py`'s, because the FK's `ON DELETE CASCADE` never
    fires - dive deletion is application-level (`is_deleted`), so no `DELETE FROM dive` ever
    runs. The same trap `delete_files_for_dive` exists to work around.

    **One row deletion is deliberately elsewhere**, and it is the exception that says what
    `delete_recording` is for: `services/dive_merge.py` folds two records of one dive into
    one recording and deletes the absorbed row directly, having first moved its files onto
    the survivor. `delete_recording` would read those files' storage keys and unlink the
    blobs after the commit, which is exactly wrong for bytes that just moved.
    """

    __tablename__ = "dive_recording"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    dive_id: Mapped[int] = mapped_column(ForeignKey("dive.id", ondelete="CASCADE"), index=True)
    # Denormalized off `dive` - see the class docstring. Not a redundant copy that could
    # drift: the two writes that create one both take the owner from the dive they resolved,
    # and the one write that moves a recording between dives (`services/dive_merge.py`) is
    # within one account by construction, both dives having been resolved through
    # `fetch_owned_or_raise` against the same caller. That is the invariant, and it is
    # narrower than the one stated here until the merge landed: "nothing moves a recording
    # between dives" was true then and is not now.
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    # Position among this dive's recordings; 0 is primary. Unique with `dive_id`, so two
    # recordings can never claim the same slot even momentarily - which is what makes the
    # promotion on deletion (and the `PATCH .../recording/{rid}` swap) a sequence of
    # statements rather than a lock.
    ordinal: Mapped[int] = mapped_column(Integer)

    # What recorded it, as the file named it - the six members of `ParsedDevice`, stored as
    # read and compared case-folded (see `services/dive_recordings.py::same_device`). All
    # nullable, because no format carries all six and because a recording that logbook
    # import created before this table existed has none of them.
    #
    # `brand`, not `manufacturer`: the published format uses that word for a maker on a
    # device and on a gear item alike, and `gear_item.brand` beside it already did.
    device_brand: Mapped[str | None] = mapped_column(String(64), default=None)
    device_model: Mapped[str | None] = mapped_column(String(64), default=None)
    # Opaque and never parsed. The one member that settles the identity question outright:
    # two files carrying equal serials are one computer, two carrying different ones are
    # two, and nothing else in a dive-computer export is that decisive.
    device_serial: Mapped[str | None] = mapped_column(String(64), default=None)
    device_firmware: Mapped[str | None] = mapped_column(String(32), default=None)
    # What the computer calls itself, as its owner set it - `Porvoo` on the Ocean in the
    # corpus. Deliberately not `device_model`: the same machine's FIT export names the
    # model `Suunto Ocean`, and folding the two would either lose the model or claim the
    # diver named their computer after it.
    device_name: Mapped[str | None] = mapped_column(String(64), default=None)
    # The *computer's* own counter, not the diver's numbering - `dive.dive_number` is that.
    # `0` is a real count (a computer that has recorded no dive yet writes it), which is why
    # this is nullable rather than defaulting to zero.
    device_dive_number: Mapped[int | None] = mapped_column(Integer, default=None)

    # The device's own start, split exactly as the dive's is: the instant in `start_time`
    # and the offset it was expressed in beside it, with NULL meaning "the source recorded a
    # wall clock and no offset" (DiveJSON §5.2). A second computer starts when its diver's
    # wrist goes under, not when the first one's did, so this is the recording's own and not
    # a copy of the dive's. See `core/utils/datetime_offset.py`, and the *Clocks* rule in
    # `services/dive_recordings.py` for why the gates compare wall clocks whenever either
    # side's offset is unknown.
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    utc_offset_minutes: Mapped[int | None] = mapped_column(Integer, default=None)

    # **For the match gates, and never shown as the dive's.** Their provenance is the path
    # that wrote them and the two genuinely differ by it: on the attach path they are the
    # device's own logged figures off the parse, and on the import path they are computed
    # from the recording's samples, because a DiveJSON Recording carries no scalars of its
    # own to read them from. The same Suunto file is 3051 seconds one way and 3473 the
    # other, so the column holds *a duration the gate can compare* rather than one number
    # with one meaning. NULL where an imported recording has no profile.
    duration: Mapped[int | None] = mapped_column(Integer, default=None)
    max_depth: Mapped[float | None] = mapped_column(Float, default=None)

    @declared_attr.directive
    @classmethod
    def __table_args__(cls) -> tuple:
        return (
            # One recording per slot per dive. What used to be `ux_dive_file_dive_id`'s job
            # - stopping a dive from accumulating records nobody asked for - moved here and
            # became "in a defined order" instead of "at most one".
            Index("ux_dive_recording_dive_id_ordinal", "dive_id", "ordinal", unique=True),
            # The match query's index, and the reason `user_id` is on this table at all.
            # Candidates are the account's recordings whose stored `start_time` lies within
            # twenty-six hours of an incoming one's - twelve hours of fuzz, wider than any
            # gate can reach, plus the fourteen an offset can move a wall clock from its
            # instant - and the gates then compute the right delta per pair in Python.
            Index("ix_dive_recording_user_id_start_time", "user_id", "start_time"),
        )
