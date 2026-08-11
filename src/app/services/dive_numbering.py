"""Dive numbering: the number to suggest for a new dive, the state of a user's existing
numbering, and the bulk renumber that tidies it.

`dive_number` is a **label**, not an identity and not an ordering key. Chronology is
owned by `start_time` everywhere in the app - `_cached_read_dives` sorts by it,
`gas_use_history` sorts by it, and `ix_dive_user_id_start_time` exists to serve exactly
that. Nothing reads `dive_number` except to print it.

That's what makes it safe to leave numbering unconstrained, which it has to be, because
the two things a diver wants from it are in direct conflict:

- Gaps are real data. A diver whose first 46 dives are in a paper logbook starts this
  log at #47, and #100-149 may stay on paper forever. Normalizing that to 1..N would
  destroy information.
- A fully backfilled log should read 1..N with nothing missing.

Both are served by never renumbering automatically, and offering the diver an explicit
renumber instead. So: `suggest_dive_number` proposes (the form can overwrite it),
`summarize_numbering` reports (the log shows it, and the diver can ignore it), and
`renumber_dives` only ever runs when asked. Nothing here is enforced at write time -
there is deliberately no unique constraint on `(user_id, dive_number)`, since duplicates
are a normal transient state mid-backfill. See DECISIONS.md.
"""

from datetime import UTC, datetime

from sqlalchemy import ColumnElement, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.utils.datetime_offset import combine_start_time, split_start_time
from ..models.dive import Dive
from ..schemas.dive import (
    DiveNumberingSummary,
    DiveNumberSuggestion,
    DiveRenumberChange,
    DiveRenumberResult,
)


def _live_dives_of(user_id: int) -> tuple[ColumnElement[bool], ...]:
    """The criteria every query here starts from: one user's non-deleted dives, which is
    also the shape `ix_dive_user_id_start_time` covers."""
    return (Dive.user_id == user_id, Dive.is_deleted.is_(False))


# Both `suggest_dive_number` and `renumber_dives` order dives by `(start_time, id)`. The
# `id` tie-break matters: two dives can share a start time (a computer that records to
# the minute, a repetitive dive entered twice by hand), and without it Postgres is free
# to return them in either order - which would let a suggestion disagree with the
# renumber that follows it, for no reason a diver could see.
_CHRONOLOGICAL = (Dive.start_time, Dive.id)


async def suggest_dive_number(db: AsyncSession, user_id: int, start_time: datetime) -> DiveNumberSuggestion:
    """The number to prefill for a dive starting at `start_time` (an offset-aware instant).

    The rule is *positional*, not "highest so far plus one": take the number of the dive
    that chronologically precedes this one and add 1. For the ordinary case - logging the
    dive you just did - the two are the same. They diverge exactly where the naive rule
    is wrong: back-filling a dive from 2019 into a log that already reaches #212 should
    suggest #12, not #213.

    Falls back to 1 when nothing precedes it, which is also what a brand-new log gets.

    The suggestion is returned even when it collides with an existing dive (the caller is
    told, via `is_taken`, but not stopped). Deliberate: back-filling a run of old dives
    produces collisions by construction, and the alternative - hunting for the next free
    number - would hand out numbers with no relation to where the dive sits. Numbering is
    reconciled at the end, in one pass, by `renumber_dives`.
    """
    utc_start_time, _ = split_start_time(start_time)

    # Served by `ix_dive_user_id_start_time` (user_id, start_time desc, partial on
    # `is_deleted = false`) as a one-row index scan - the reason this is a positional
    # lookup rather than a `MAX(dive_number)` scan over the whole log.
    predecessor = await db.scalar(
        select(Dive.dive_number)
        .where(*_live_dives_of(user_id), Dive.start_time <= utc_start_time)
        .order_by(Dive.start_time.desc(), Dive.id.desc())
        .limit(1)
    )

    dive_number = 1 if predecessor is None else predecessor + 1

    # No index on `dive_number`, so this filters the user's dives on the heap. Left that
    # way on purpose: it's one short-circuiting `EXISTS` on a form load over a few hundred
    # rows, and an index on a column nothing filters or sorts by in production would be
    # paid for on every dive write to serve this one advisory check.
    is_taken = await db.scalar(select(exists().where(*_live_dives_of(user_id), Dive.dive_number == dive_number)))

    return DiveNumberSuggestion(dive_number=dive_number, is_taken=bool(is_taken))


async def summarize_numbering(db: AsyncSession, user_id: int) -> DiveNumberingSummary:
    """Describe the state of a user's numbering, in one pass over their dives.

    Everything comes from a single windowed aggregate rather than several round trips,
    since the caller (the log's numbering indicator) always wants the whole picture.
    """
    # `LAG` over chronological order is what makes "numbered out of date order" answerable
    # at all: it compares each dive's number with that of the dive actually before it,
    # rather than against min/max, so a single dive misnumbered in the middle is visible.
    # Strictly `<` - a dive repeating its predecessor's number is a duplicate, counted as
    # such below, and reporting it twice would overstate how tangled the log is.
    ordered = (
        select(
            Dive.dive_number,
            func.lag(Dive.dive_number).over(order_by=_CHRONOLOGICAL).label("previous_number"),
        )
        .where(*_live_dives_of(user_id))
        .cte("ordered")
    )

    total_dives, lowest, highest, distinct_numbers, out_of_date_order_count = (
        await db.execute(
            select(
                func.count(),
                func.min(ordered.c.dive_number),
                func.max(ordered.c.dive_number),
                func.count(ordered.c.dive_number.distinct()),
                func.count().filter(ordered.c.dive_number < ordered.c.previous_number),
            ).select_from(ordered)
        )
    ).one()

    if total_dives == 0:
        return DiveNumberingSummary(
            total_dives=0,
            lowest=None,
            highest=None,
            missing_count=0,
            duplicate_count=0,
            out_of_date_order_count=0,
            # An empty log is not "sequential" - there is nothing to be sequential about,
            # and the indicator has nothing to say until the first dive is logged.
            is_sequential=False,
        )

    # Distinct, not total: three dives sharing #7 occupy one slot in the run.
    missing_count = (highest - lowest + 1) - distinct_numbers
    duplicate_count = total_dives - distinct_numbers

    return DiveNumberingSummary(
        total_dives=total_dives,
        lowest=lowest,
        highest=highest,
        missing_count=missing_count,
        duplicate_count=duplicate_count,
        out_of_date_order_count=out_of_date_order_count,
        # Note what this doesn't require: starting at 1. A log running #47-#212 with
        # nothing missing is exactly as tidy as one running #1-#166, and telling a diver
        # otherwise would be telling them their paper logbook is wrong.
        is_sequential=missing_count == 0 and duplicate_count == 0,
    )


async def renumber_dives(
    db: AsyncSession,
    user_id: int,
    *,
    start_at: int = 1,
    from_start_time: datetime | None = None,
    dry_run: bool = False,
) -> DiveRenumberResult:
    """Renumber a user's dives consecutively from `start_at`, in chronological order.

    Only ever called from the renumber endpoint, which a diver reaches deliberately -
    see this module's docstring for why this is never automatic.

    `from_start_time` limits the scope to dives at or after that instant. That's what
    lets a diver keep the part of their log that mirrors a paper logbook and tidy only
    the tail: "renumber everything from 2023 onwards, starting at #47".

    `dry_run` computes the same change list and writes nothing, so the confirmation
    dialog and the write it confirms are produced by identical code - a preview that can
    disagree with what follows is worse than no preview.
    """
    scope = list(_live_dives_of(user_id))
    if from_start_time is not None:
        utc_from, _ = split_start_time(from_start_time)
        scope.append(Dive.start_time >= utc_from)

    # `start_at - 1` because `row_number()` is 1-based.
    new_number = func.row_number().over(order_by=_CHRONOLOGICAL) + (start_at - 1)

    rows = (
        await db.execute(
            select(
                Dive.uuid,
                Dive.dive_number,
                Dive.start_time,
                Dive.utc_offset_minutes,
                new_number.label("new_dive_number"),
            )
            .where(*scope)
            .order_by(*_CHRONOLOGICAL)
        )
    ).all()

    changes = [
        DiveRenumberChange(
            dive_uuid=row.uuid,
            # Re-attached to the dive's own offset, as everywhere else a dive's time
            # leaves this app - the preview lists dives by date, and they have to read
            # the same as they do on the dive list.
            start_time=combine_start_time(row.start_time, row.utc_offset_minutes),
            dive_number=row.dive_number,
            new_dive_number=row.new_dive_number,
        )
        for row in rows
        if row.dive_number != row.new_dive_number
    ]

    if not dry_run and changes:
        # One `UPDATE ... FROM` over a CTE rather than a write per dive: renumbering a
        # whole log has to be all-or-nothing, since a partial run would leave numbering in
        # a state neither the diver nor `summarize_numbering` could make sense of. It also
        # sidesteps the collisions a row-by-row pass would hit halfway through shifting a
        # run of numbers down by one.
        renumbered = (
            select(Dive.id.label("dive_id"), new_number.label("new_dive_number")).where(*scope).cte("renumbered")
        )
        await db.execute(
            update(Dive)
            .where(Dive.id == renumbered.c.dive_id, Dive.dive_number != renumbered.c.new_dive_number)
            .values(dive_number=renumbered.c.new_dive_number, updated_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        await db.commit()

    return DiveRenumberResult(dry_run=dry_run, dives_in_scope=len(rows), changes=changes)
