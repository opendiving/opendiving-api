"""Folding two dives into one: which survives, what moves, and how two records land on one axis.

**Why this exists at all.** A dive computer that shuts down mid-water - a battery, a flooded
button, a diver who surfaced to reposition the boat - logs the dive as two records, and every
path into this app then makes it two dives: the parse form offers one at a time, and logbook
import refuses to fold them by design (one device's two records of one dive is the case the
strict gate deliberately never admits). Merging is the diver saying *these two are one*, which
is the one thing no gate can decide from the data.

**The earlier dive survives and the later one is soft-deleted**, after everything on it moves.
Nothing here is undone: the later dive's uuid stops resolving, and a diver who merges the wrong
pair has to rebuild the second dive by hand. That is the same bargain `DELETE /dive/{uuid}`
already offers, and the route says so.

**Two branches, and the device test picks between them.** Two records of *one computer* fold
into one recording - the later record's samples offset onto the earlier's clock, the gap where
the computer was off left as a gap. Two *different* computers were both recording the whole
time, so their recordings simply sit side by side on the surviving dive, which is Subsurface's
*join* rather than its merge.
"""

import uuid as uuid_pkg
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..core.schemas import NOTES_MAX_LENGTH
from ..crud.crud_dive_mixtures import get_mixtures_for_dive, replace_mixtures_for_dive
from ..crud.crud_dives import crud_dives
from ..models.dive import Dive
from ..models.dive_dive_site import DiveDiveSite
from ..models.dive_file import DiveFile
from ..models.dive_gear_item import DiveGearItem
from ..models.dive_profile import DiveProfile
from ..models.dive_recording import DiveRecording
from ..models.dive_species import DiveSpecies
from ..schemas.dive import DiveReadInternal
from ..schemas.dive_mixture import as_create
from ..schemas.dive_profile import DEPTH_SCALE, MILLISECONDS_PER_SECOND
from .dive_files import apply_gas_mapping, relabel_gas_numbers
from .dive_profiles import (
    MERGE_PARSER_KEY,
    NormalizedProfile,
    attribute_and_cap,
    delete_profile_for_recording,
    join_profiles,
    load_stored_profile,
    profile_payload_digest,
    replace_profile_samples,
    shift_profile,
    store_profile,
)
from .dive_recordings import (
    READOUT_COLUMNS,
    DeviceIdentity,
    delta_seconds,
    next_ordinal,
    same_device,
    starts_before,
    wall_clock,
)


class DiveNotMergeableError(Exception):
    """These two dives cannot be folded into one, and the message says which and why."""


@dataclass(frozen=True, slots=True)
class MergedDives:
    """What the merge did, for the route to answer with."""

    survivor_id: int
    survivor_uuid: uuid_pkg.UUID
    removed_uuid: uuid_pkg.UUID
    # Whether the two dives' primary recordings turned out to be one computer's two records
    # of one dive and were folded into a single recording. False means the two recordings
    # sit side by side on the surviving dive instead - a second computer, or a pair this
    # could not place on one clock.
    folded: bool


@dataclass(frozen=True, slots=True)
class _Recording:
    """One recording as the merge reasons about it: where it sits and what recorded it."""

    id: int
    ordinal: int
    start_time: datetime | None
    utc_offset_minutes: int | None
    device: DeviceIdentity


@dataclass(frozen=True, slots=True)
class _Span:
    """One recording's place on the dive's clock and what its samples cover.

    The input to `dive_figures`, and deliberately not `_Recording` plus a lookup: the folded
    recording's span is computed rather than read, so the figures have to be derivable from
    values the caller holds rather than from rows it would have to write first.
    """

    start_time: datetime | None
    utc_offset_minutes: int | None
    # The profile's span, in the axis's milliseconds.
    duration: int | None
    max_depth_cm: int | None


# ---------------------------------------------------------------- pure, DB-free


def dive_figures(spans: Sequence[_Span]) -> tuple[int | None, float | None]:
    """A dive's `duration` in seconds and its `max_depth`, from what its recordings carry.

    **`duration` runs from the earliest recording's start to the last sample any of them
    recorded**, which is not the same as the later dive's own logged end and is deliberately
    the sampled figure. The corpus pair is the argument: two halves of one Perdix dive 223 s
    apart whose second half samples 2 940 s gives 3 163, while its *logged* 2 921 would give
    3 144 - and the samples are what the merged profile actually contains, so a dive claiming
    3 144 would be claiming a span its own chart runs past.

    `max_depth` is the deepest reading across every recording, whether or not it could be
    placed on the clock: a depth is a reading rather than an instant, and needs no axis.

    **A recording with no start, or with no samples, contributes to the depth and not to the
    span.** There is nowhere to put it on the axis, and treating an unplaceable record as
    starting at zero would make the dive claim a span nothing supports.

    Either figure is `None` when nothing carries it, which is the caller's signal to leave
    the dive's own value alone rather than to clear it.
    """
    placed = [
        (span.start_time, span.utc_offset_minutes, span.duration)
        for span in spans
        if span.start_time is not None and span.duration is not None
    ]
    duration = None
    if placed:
        origin_start, origin_offset, _ = min(placed, key=_clock_key(placed))
        duration = max(
            round(delta_seconds(origin_start, origin_offset, start, offset) + covered / MILLISECONDS_PER_SECOND)
            for start, offset, covered in placed
        )

    depths = [span.max_depth_cm for span in spans if span.max_depth_cm is not None]
    return duration, (max(depths) / DEPTH_SCALE if depths else None)


_Placed = tuple[datetime, int | None, int]


def _clock_key(placed: Sequence[_Placed]) -> Callable[[_Placed], datetime]:
    """The sort key that puts a set of starts in order, under the *Clocks* rule.

    That rule is pairwise - instants where both sides carry an offset, wall clocks where
    either does not - and pairwise is not an ordering over three or more: a set holding one
    offset-less recording would compare *some* of its pairs as instants and some as clock
    faces, which can disagree about which is earliest. Degenerating the whole set to one
    clock is what the rule already says for every pair that involves the offset-less one, so
    that is what a set does: instants when every member carries an offset, clock faces
    otherwise. Two members is the pairwise rule exactly, which is the case the merge itself
    always asks about.
    """
    if all(offset is not None for _, offset, _ in placed):
        return lambda item: item[0]
    return lambda item: wall_clock(item[0], item[1])


def merged_notes(survivor: str, absorbed: str, *, dive_number: int) -> str:
    """The surviving dive's notes with the other dive's appended under a line naming it.

    The other dive stops resolving, so its notes have nowhere else to go, and a diver who
    wrote "lost the group at the wreck" on the second half wrote it about this dive. The
    heading is what keeps the two apart afterwards - appending them bare would read as one
    continuous entry that the diver never wrote.

    **Nothing is written when the other dive had no notes.** A heading over an empty section
    is noise in a field the diver owns, and the merge is recorded on the dive's recordings
    rather than in its prose.

    Truncated to the same `NOTES_MAX_LENGTH` every notes field carries, because two dives
    each within the limit are not: without this the merged dive would be unreadable through
    its own read schema, which validates the length.
    """
    if not absorbed.strip():
        return survivor
    heading = f"Notes from dive {dive_number}, merged into this one:"
    joined = f"{survivor.rstrip()}\n\n{heading}\n{absorbed}" if survivor.strip() else f"{heading}\n{absorbed}"
    return joined[:NOTES_MAX_LENGTH]


# ---------------------------------------------------------------- persistence


async def merge_dives(db: AsyncSession, *, first: DiveReadInternal, second: DiveReadInternal) -> MergedDives:
    """Fold two of one diver's dives into one. Does **not** commit - the caller owns that.

    Both dives must already have been resolved as the caller's own; nothing here re-checks
    ownership, on `store_recording_file`'s terms.

    Raises `DiveNotMergeableError` when either dive was entered by hand - a dive with no
    recording has nothing this could fold, and Subsurface refuses the same pair in
    `likely_same` - or when the surviving dive's own average depth would end up deeper than
    the maximum the merged recordings actually reached.
    """
    survivor, absorbed = (first, second) if _orders_first(first, second) else (second, first)

    survivor_recordings = await _recordings(db, dive_id=survivor.id)
    absorbed_recordings = await _recordings(db, dive_id=absorbed.id)
    for dive, recordings in ((survivor, survivor_recordings), (absorbed, absorbed_recordings)):
        if not recordings:
            raise DiveNotMergeableError(
                f"Dive {dive.dive_number} was entered by hand and has no dive-computer recording, so there is "
                "nothing to merge. Merging folds two records of one dive together."
            )

    # **Before anything is written**, because the labels the absorbed half's pressure
    # channels are stored under have to be rewritten as those channels move - and in the
    # folded case they are rewritten *into* the surviving recording's own samples, where
    # there would be no telling them apart afterwards.
    survivor_mixtures = await get_mixtures_for_dive(db=db, dive_id=survivor.id)
    absorbed_mixtures = await get_mixtures_for_dive(db=db, dive_id=absorbed.id)
    mapping, appended = relabel_gas_numbers(absorbed_mixtures, survivor_mixtures)

    primary_survivor, primary_absorbed = survivor_recordings[0], absorbed_recordings[0]
    folded = _folds(primary_survivor, primary_absorbed)

    fold: _Fold | None = None
    if folded:
        fold = await _plan_fold(db, survivor=primary_survivor, absorbed=primary_absorbed, mapping=mapping)

    moving = absorbed_recordings[1:] if folded else absorbed_recordings
    spans = [
        *(await _spans(db, recordings=survivor_recordings, replacing=fold)),
        *(await _spans(db, recordings=moving)),
    ]
    duration, max_depth = dive_figures(spans)
    _refuse_a_depth_the_dive_contradicts(survivor, max_depth)

    if appended:
        await replace_mixtures_for_dive(
            db=db,
            dive_id=survivor.id,
            mixtures=[
                *(as_create(row) for row in survivor_mixtures),
                *(as_create(row) for row in appended),
            ],
            commit=False,
        )

    if fold is not None:
        await _write_fold(db, fold, dive_id=survivor.id)
    await _move_recordings(db, moving, dive_id=survivor.id, mapping=mapping)
    await _move_links(db, from_dive_id=absorbed.id, to_dive_id=survivor.id)

    values: dict[str, object] = {
        "notes": merged_notes(survivor.notes, absorbed.notes, dive_number=absorbed.dive_number)
    }
    # `duration` has a `> 0` check on the table, so a merge of two profile-less recordings -
    # which yields no span at all - leaves the diver's own figure alone rather than writing a
    # zero the row would refuse.
    if duration:
        values["duration"] = duration
    if max_depth is not None:
        values["max_depth"] = max_depth
    await db.execute(update(Dive).where(Dive.id == survivor.id).values(**values))

    await crud_dives.delete(db=db, uuid=absorbed.uuid, commit=False)

    return MergedDives(survivor_id=survivor.id, survivor_uuid=survivor.uuid, removed_uuid=absorbed.uuid, folded=folded)


def _orders_first(left: DiveReadInternal, right: DiveReadInternal) -> bool:
    """Which of two dives is the earlier, and so the one that survives.

    The *Clocks* rule, with the row id as the tie-break: two dives whose starts are equal on
    the comparison clock have to resolve to the same survivor whichever order the request
    named them in, or the same two uuids would merge two different ways.
    """
    if starts_before(left.start_time, left.utc_offset_minutes, right.start_time, right.utc_offset_minutes):
        return True
    if starts_before(right.start_time, right.utc_offset_minutes, left.start_time, left.utc_offset_minutes):
        return False
    return left.id < right.id


def _folds(survivor: _Recording, absorbed: _Recording) -> bool:
    """Whether these two primary recordings are one computer's two records of one dive.

    The device test of `services/dive_recordings.py`, and **both starts**. The device half is
    decision-grade on its own; the starts are what makes a fold expressible at all, since the
    samples of one record are placed on the other's axis by the delta between the two starts
    and there is no delta without them.

    A same-device pair this cannot place on the clock takes the other branch and the two
    recordings sit side by side on the surviving dive. That is the honest answer rather than a
    refusal: nothing is lost, both records keep their own samples, and the diver can delete
    whichever they do not want - where refusing would block a merge whose sites, gear, species
    and notes are perfectly mergeable over a clock reading nobody can supply.
    """
    return same_device(survivor.device, absorbed.device) and (
        survivor.start_time is not None and absorbed.start_time is not None
    )


@dataclass(frozen=True, slots=True)
class _Fold:
    """Two records of one computer, resolved onto one axis and ready to write."""

    # The recording that survives - always the surviving *dive's* primary, whichever of the
    # two records started first.
    recording_id: int
    # The one whose files move onto it and whose row then goes.
    absorbed_recording_id: int
    # The start the merged samples are elapsed from: the earlier of the two records', which
    # is not always the surviving dive's own, a diver's logged start and a computer's not
    # being the same number.
    start_time: datetime
    utc_offset_minutes: int | None
    profile: NormalizedProfile | None


async def _plan_fold(db: AsyncSession, *, survivor: _Recording, absorbed: _Recording, mapping: dict[int, int]) -> _Fold:
    """Put both records' samples on one axis, with the gap between them left as a gap.

    **The offset applied is the two recordings' start delta, never the two dives'.** A dive's
    `start_time` is the diver's logbook entry and a recording's is the device's own stamp;
    using the former would slide one half of the profile by whatever the diver typed.

    **The axis origin is the earlier of the two *records*, which need not belong to the
    surviving dive.** The surviving dive is the one that started first by its own logged
    clock, and a diver who logged the two halves in either order still gets samples that run
    forwards: where the surviving dive's recording is the later of the two, it is that one's
    samples that move and the surviving recording's own start moves back.

    The absorbed half's cylinder labels are rewritten through `mapping` **before** the join,
    because afterwards its pressure channels are indistinguishable from the survivor's own.
    """
    survivor_start, absorbed_start = survivor.start_time, absorbed.start_time
    if survivor_start is None or absorbed_start is None:  # pragma: no cover - `_folds` said otherwise
        raise DiveNotMergeableError("Neither recording records when it started, so its samples cannot be placed.")

    offset = round(
        delta_seconds(survivor_start, survivor.utc_offset_minutes, absorbed_start, absorbed.utc_offset_minutes)
        * MILLISECONDS_PER_SECOND
    )
    survivor_first = not starts_before(
        absorbed_start, absorbed.utc_offset_minutes, survivor_start, survivor.utc_offset_minutes
    )

    survivor_stored = await load_stored_profile(db, recording_id=survivor.id)
    absorbed_stored = await load_stored_profile(db, recording_id=absorbed.id)
    survivor_profile = None if survivor_stored is None else survivor_stored.profile
    absorbed_profile = None if absorbed_stored is None else apply_gas_mapping(absorbed_stored.profile, mapping)

    if survivor_first:
        earlier, later = survivor_profile, absorbed_profile
        origin_start, origin_offset = survivor_start, survivor.utc_offset_minutes
    else:
        earlier, later = absorbed_profile, survivor_profile
        origin_start, origin_offset = absorbed_start, absorbed.utc_offset_minutes

    joined = join_profiles(earlier, None if later is None else shift_profile(later, offset))
    return _Fold(
        recording_id=survivor.id,
        absorbed_recording_id=absorbed.id,
        start_time=origin_start,
        utc_offset_minutes=origin_offset,
        profile=attribute_and_cap(joined),
    )


async def _write_fold(db: AsyncSession, fold: _Fold, *, dive_id: int) -> None:
    """Land the folded record: its files, its samples, its start and its gate figures.

    **The absorbed recording's files move rather than going with its row**, which is the
    point of keeping them: nothing on this instance can re-derive folded samples, so the
    files either half held are the only evidence left of what the computer wrote, and they
    stay downloadable. The row itself is then deleted directly rather than through
    `delete_recording` - that one reads the storage keys first and unlinks the blobs after
    the commit, which is exactly the wrong thing for bytes that just moved.

    **The surviving recording's own `duration` and `max_depth` are recomputed from the merged
    profile.** They are the columns the match gates compare, and after a fold the recording
    describes both records; leaving them at the surviving half's figures would have every
    later gate comparing an incoming file against half a dive. The column is seconds, so the
    span is divided into it.

    **The absorbed recording's readouts and salinity fill the surviving one's blanks** before
    its row goes, on the rule every second record of one recording follows: fill, never
    overwrite.
    """
    await db.execute(
        update(DiveFile)
        .where(DiveFile.recording_id == fold.absorbed_recording_id)
        .values(recording_id=fold.recording_id, dive_id=dive_id)
    )
    absorbed = aliased(DiveRecording)
    await db.execute(
        update(DiveRecording)
        .where(DiveRecording.id == fold.recording_id, absorbed.id == fold.absorbed_recording_id)
        .values(
            {
                getattr(DiveRecording, column): func.coalesce(getattr(DiveRecording, column), getattr(absorbed, column))
                for column in (*READOUT_COLUMNS, "salinity")
            }
        )
    )
    await db.execute(delete(DiveRecording).where(DiveRecording.id == fold.absorbed_recording_id))

    if fold.profile is None:
        # Neither half recorded a sample, so there is nothing folded to protect and the
        # recording's profile is once again whatever its files yield - which is what the
        # backfill will work out. Any stored row goes, rather than being left describing one
        # of the two records.
        await delete_profile_for_recording(db, recording_id=fold.recording_id, commit=False)
    else:
        await store_profile(
            db,
            recording_id=fold.recording_id,
            dive_id=dive_id,
            profile=fold.profile,
            source_sha256=profile_payload_digest(fold.profile),
            parser_key=MERGE_PARSER_KEY,
            commit=False,
        )

    depths = fold.profile.depth.v if fold.profile is not None and fold.profile.depth else []
    await db.execute(
        update(DiveRecording)
        .where(DiveRecording.id == fold.recording_id)
        .values(
            start_time=fold.start_time,
            utc_offset_minutes=fold.utc_offset_minutes,
            duration=None if fold.profile is None else round(fold.profile.duration / MILLISECONDS_PER_SECOND),
            max_depth=max(depths) / DEPTH_SCALE if depths else None,
        )
    )


async def _move_recordings(
    db: AsyncSession, recordings: Sequence[_Recording], *, dive_id: int, mapping: dict[int, int]
) -> None:
    """Re-point whole recordings at the surviving dive, appended after its last.

    Their samples are untouched and unshifted: a recording's `times` are elapsed from its own
    start, which moves with it, so the only thing that changes is which dive the row belongs
    to. What *does* change is the labelling - a second computer numbers its tanks its own way,
    and those numbers now have to mean the surviving dive's cylinders.

    `dive_id` is denormalized onto `dive_file` and `dive_profile` as a read key, so both
    follow. Nothing here touches `user_id`: a merge is within one account by construction.
    """
    ordinal = await next_ordinal(db, dive_id=dive_id)
    for position, recording in enumerate(recordings):
        await db.execute(
            update(DiveRecording)
            .where(DiveRecording.id == recording.id)
            .values(dive_id=dive_id, ordinal=ordinal + position)
        )
        await db.execute(update(DiveFile).where(DiveFile.recording_id == recording.id).values(dive_id=dive_id))
        await db.execute(update(DiveProfile).where(DiveProfile.recording_id == recording.id).values(dive_id=dive_id))
        if mapping:
            stored = await load_stored_profile(db, recording_id=recording.id)
            if stored is not None:
                remapped = apply_gas_mapping(stored.profile, mapping)
                if remapped is not None:
                    await replace_profile_samples(db, recording_id=recording.id, profile=remapped)


async def _move_links(db: AsyncSession, *, from_dive_id: int, to_dive_id: int) -> None:
    """Carry the other dive's sites, gear and species across, skipping what is already there.

    One statement per table, because each carries a `(dive_id, x_id)` uniqueness constraint
    and the two dives may name the same site or the same wing: the rows that would collide are
    left where they are and go down with the dive, and the rest are re-pointed and appended
    after the surviving dive's own. Deleting the collisions instead would buy nothing - the
    dive they belong to is about to stop resolving.

    `position` is rewritten rather than kept, so the surviving dive's own list stays first and
    the incoming one follows in its own order.
    """
    for model, reference in _LINKED_COLLECTIONS:
        highest = (
            await db.execute(select(func.max(model.position)).where(model.dive_id == to_dive_id))
        ).scalar_one_or_none()
        next_position = 0 if highest is None else int(highest) + 1
        moving = (
            (
                await db.execute(
                    select(model.id)
                    .where(
                        model.dive_id == from_dive_id,
                        reference.not_in(select(reference).where(model.dive_id == to_dive_id)),
                    )
                    .order_by(model.position, model.id)
                )
            )
            .scalars()
            .all()
        )
        for offset, row_id in enumerate(moving):
            await db.execute(
                update(model).where(model.id == row_id).values(dive_id=to_dive_id, position=next_position + offset)
            )


# The three collections a dive links rather than owns, each with the column that says which
# row it points at - the half of its `(dive_id, x_id)` uniqueness constraint that decides
# whether the surviving dive already has this one.
_LINKED_COLLECTIONS = (
    (DiveDiveSite, DiveDiveSite.dive_site_id),
    (DiveGearItem, DiveGearItem.gear_item_id),
    (DiveSpecies, DiveSpecies.species_id),
)


def _refuse_a_depth_the_dive_contradicts(dive: DiveReadInternal, max_depth: float | None) -> None:
    """Stop before writing a maximum shallower than the dive's own average depth.

    `ck_dive_avg_depth_within_max` would otherwise refuse the write from inside the
    transaction, and an `IntegrityError` there is a 500 rather than anything a diver can act
    on. The pair can only disagree when the average was typed deeper than either recording
    ever went, so the number to change is the average - which is the diver's, on a form they
    can reach, and not this route's to silently drop.
    """
    if max_depth is None or dive.avg_depth is None or dive.avg_depth <= max_depth:
        return
    raise DiveNotMergeableError(
        f"Dive {dive.dive_number} records an average depth of {dive.avg_depth} m, which is deeper than the "
        f"{max_depth} m the merged recordings reached. Correct the average depth first, then merge."
    )


async def _recordings(db: AsyncSession, *, dive_id: int) -> list[_Recording]:
    """One dive's recordings in ordinal order, with what the gates compare devices on.

    Explicit columns rather than entities, on this layer's standing discipline: what a
    decision reads should be visible in the statement that reads it.
    """
    stmt = (
        select(
            DiveRecording.id,
            DiveRecording.ordinal,
            DiveRecording.start_time,
            DiveRecording.utc_offset_minutes,
            DiveRecording.device_brand,
            DiveRecording.device_model,
            DiveRecording.device_serial,
            DiveRecording.device_dive_number,
        )
        .where(DiveRecording.dive_id == dive_id)
        .order_by(DiveRecording.ordinal)
    )
    return [
        _Recording(
            id=row.id,
            ordinal=row.ordinal,
            start_time=row.start_time,
            utc_offset_minutes=row.utc_offset_minutes,
            device=DeviceIdentity(
                brand=row.device_brand,
                model=row.device_model,
                serial=row.device_serial,
                dive_number=row.device_dive_number,
            ),
        )
        for row in await db.execute(stmt)
    ]


async def _spans(db: AsyncSession, *, recordings: Sequence[_Recording], replacing: _Fold | None = None) -> list[_Span]:
    """What each of these recordings covers, for `dive_figures`.

    `replacing` is the fold, whose recording is about to stop describing what it describes
    now: its span is taken from the merged profile in hand rather than from the row, because
    the row has not been written yet and the figures are checked before anything is.
    """
    stmt = select(DiveProfile.recording_id, DiveProfile.duration, DiveProfile.max_depth_cm).where(
        DiveProfile.recording_id.in_({recording.id for recording in recordings})
    )
    figures = {row.recording_id: (row.duration, row.max_depth_cm) for row in await db.execute(stmt)}

    spans: list[_Span] = []
    for recording in recordings:
        if replacing is not None and recording.id == replacing.recording_id:
            depths = replacing.profile.depth.v if replacing.profile is not None and replacing.profile.depth else []
            spans.append(
                _Span(
                    start_time=replacing.start_time,
                    utc_offset_minutes=replacing.utc_offset_minutes,
                    duration=None if replacing.profile is None else replacing.profile.duration,
                    max_depth_cm=max(depths) if depths else None,
                )
            )
            continue
        duration, max_depth_cm = figures.get(recording.id, (None, None))
        spans.append(
            _Span(
                start_time=recording.start_time,
                utc_offset_minutes=recording.utc_offset_minutes,
                duration=duration,
                max_depth_cm=max_depth_cm,
            )
        )
    return spans
