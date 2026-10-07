import uuid as uuid_pkg
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol

from fastcrud import FastCRUD
from sqlalchemy import (
    ARRAY,
    Boolean,
    ColumnElement,
    Date,
    DateTime,
    Integer,
    Interval,
    and_,
    case,
    cast,
    false,
    func,
    literal,
    literal_column,
    null,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.dive import Dive
from ..models.dive_dive_site import DiveDiveSite
from ..models.dive_gear_item import DiveGearItem
from ..models.dive_person import DivePerson
from ..models.dive_species import DiveSpecies
from ..models.dive_tag import DiveTag
from ..schemas.dive import (
    DiveCreateInternal,
    DiveDelete,
    DiveListSort,
    DiveReadInternal,
    DiveUpdate,
    DiveUpdateInternal,
)

CRUDDive = FastCRUD[Dive, DiveCreateInternal, DiveUpdate, DiveUpdateInternal, DiveDelete, DiveReadInternal]
crud_dives = CRUDDive(Dive)


def offset_of_the(order: Any) -> Any:
    """The `utc_offset_minutes` of the first dive in the group under `order`.

    **A dive displays in the timezone it was logged in**, which an aggregate over dives has to
    honour like every other dive-derived surface - `_to_public_start_time`, `dive_neighbors`,
    `dive_activity` and `gas_use_history` all reconstruct it, and `DECISIONS.md` states it as the
    API contract. A `timestamptz` stores only an absolute instant, so a dive logged at 09:00 in
    Bangkok comes back as 02:00 UTC and would read as the wrong local time - and, for an evening
    dive, the wrong day.

    It is harder for an aggregate - the life list's `first_seen` and `last_seen`, a site's
    `last_dived_on` - than anywhere else in the app: the offset wanted is the one belonging to
    the single dive that produced the `min()` or the `max()`, and no aggregate over the offset
    column can say which that was.
    Ordering `array_agg` and taking its first element is what pairs the two, in one pass over the
    group that Postgres is already making. `Dive.id` breaks a tie between two dives at the same
    instant, so a diver with two logs at one timestamp does not get a different offset run to
    run.

    The conversion itself still happens in Python, through `combine_dive_start_time`. Deliberately,
    and the same call `dive_activity` explains: `core/utils/datetime_offset.py` is documented as
    the single place that conversion happens for what is displayed or bucketed, and the failure
    mode of a second copy of it in SQL is a list that quietly disagrees with the dive pages it was
    built from. `DIVE_LOCAL_DAY` below is the one SQL copy, for a predicate, and is test-pinned.

    A NULL offset - the logbook importer's offset-unknown state - travels this path intact and
    needs no special case at either end: a Postgres array may hold NULL elements, so the
    subscript yields `None`, and the combiner reads that as "the column holds the wall clock" and
    hands it back naive. `SpeciesLifeListEntry`'s two timestamps admit that naive value, and a
    bare date too - `date_only_of_the` pairs the flag the same way.
    """
    return func.array_agg(aggregate_order_by(Dive.utc_offset_minutes, order, Dive.id.asc()), type_=ARRAY(Integer))[1]


def date_only_of_the(order: Any) -> Any:
    """`start_date_only` of the same dive `offset_of_the` picks, by the same ordering."""
    return func.array_agg(aggregate_order_by(Dive.start_date_only, order, Dive.id.asc()), type_=ARRAY(Boolean))[1]


# The list's filters by dive site, gear item, species, person or tag, each a single
# `id IN (subquery)` condition rather than a separate round trip to resolve matching dive ids.
#
# None of the subqueries scopes by owner, and that is safe rather than an omission: the
# `user_id` condition `get_dives_page` always applies is what bounds the result, and each of
# these only narrows it further. `showing_species` could not scope by owner in any case - the
# catalog is global and `species` has no `user_id` - which is exactly why it needs no migration
# either: it reads `dive_species.species_id`, already indexed, and the model comment says it was
# indexed for this.
def at_dive_site(dive_site_id: int) -> ColumnElement[bool]:
    return Dive.id.in_(select(DiveDiveSite.dive_id).where(DiveDiveSite.dive_site_id == dive_site_id))


def with_gear_item(gear_item_id: int) -> ColumnElement[bool]:
    return Dive.id.in_(select(DiveGearItem.dive_id).where(DiveGearItem.gear_item_id == gear_item_id))


def showing_species(species_id: int) -> ColumnElement[bool]:
    return Dive.id.in_(select(DiveSpecies.dive_id).where(DiveSpecies.species_id == species_id))


def with_person(person_id: int) -> ColumnElement[bool]:
    return Dive.id.in_(select(DivePerson.dive_id).where(DivePerson.person_id == person_id))


def with_tag(tag_id: int) -> ColumnElement[bool]:
    return Dive.id.in_(select(DiveTag.dive_id).where(DiveTag.tag_id == tag_id))


# The list's orders. `date` is `ix_dive_user_id_start_time`'s own. `rating` spells `NULLS LAST`
# out, which `get_multi` cannot: Postgres puts nulls first on a bare `DESC`, and an unrated dive
# above every rated one is the wrong answer - see *"The certification list spells out `NULLS
# LAST`, because `get_multi` cannot"* in DECISIONS.md. A rating sort over one diver's dives is
# a small scan, so it has no index.
_LIST_ORDERS: dict[DiveListSort, tuple[ColumnElement[Any], ...]] = {
    DiveListSort.DATE: (Dive.start_time.desc(),),
    DiveListSort.RATING: (Dive.rating.desc().nulls_last(), Dive.start_time.desc()),
}


# A dive's own local calendar day, in SQL: the stored instant read `AT TIME ZONE 'UTC'` - the
# api sets no session time zone, so a bare cast would follow the server's - plus the stored
# offset, zero where it is NULL. That one sum serves every stored state: a NULL offset holds the
# wall clock labelled UTC, and a bare date holds its midnight on the same label.
#
# For a predicate a paginated list has to apply in the database; `core/utils/datetime_offset.py`
# stays the single place a day is displayed or bucketed, and `tests/test_trip_candidates.py` pins
# this to `local_day()`. See DECISIONS.md, *"A trip's candidates are chosen by a local day
# computed in SQL"*.
DIVE_LOCAL_DAY: ColumnElement[date] = cast(
    func.timezone("UTC", Dive.start_time, type_=DateTime)
    + func.coalesce(Dive.utc_offset_minutes, 0) * literal_column("INTERVAL '1 minute'", Interval),
    Date,
)


class DatedPart(Protocol):
    """What the predicates below read of a trip's part."""

    @property
    def start_date(self) -> date | None: ...

    @property
    def end_date(self) -> date | None: ...


def _instant_window(parts: Sequence[DatedPart]) -> list[ColumnElement[bool]]:
    """Stored instants bracketing every dive whose local day a dated part covers, as
    `year_window` brackets a year: a day wider at each end, because a stored offset is under a
    day either way. A pre-filter that lets `ix_dive_user_id_start_time` bound the scan;
    `DIVE_LOCAL_DAY` decides. An open end on any part leaves that side unbounded."""
    dated = [part for part in parts if part.start_date or part.end_date]
    bounds: list[ColumnElement[bool]] = []
    starts = [part.start_date for part in dated]
    if all(starts):
        first = min(day for day in starts if day)
        bounds.append(Dive.start_time >= datetime.combine(first, time(), UTC) - timedelta(days=1))
    ends = [part.end_date for part in dated]
    if all(ends):
        last = max(day for day in ends if day)
        bounds.append(Dive.start_time < datetime.combine(last, time(), UTC) + timedelta(days=2))
    return bounds


def covered_by(parts: Sequence[DatedPart]) -> ColumnElement[bool]:
    """Whether a dive's local day is one some part covers: on or after its `start_date` where
    it has one, on or before its `end_date` where it has one. An open end is no bound - a
    trip the diver is still on collects every dive since - and a part with no dates covers
    no day."""
    arms = [
        and_(
            *([DIVE_LOCAL_DAY >= part.start_date] if part.start_date else []),
            *([DIVE_LOCAL_DAY <= part.end_date] if part.end_date else []),
        )
        for part in parts
        if part.start_date or part.end_date
    ]
    if not arms:
        return false()
    return and_(or_(*arms), *_instant_window(parts))


def candidate_of(parts: Sequence[DatedPart]) -> ColumnElement[bool]:
    """A dive on no trip whose local day one of `parts` covers - a trip's candidate, once the
    caller adds the owner and liveness every dive query carries. A dive on another trip is
    never one: the trip page is not where a dive changes trips."""
    return and_(Dive.trip_id.is_(None), covered_by(parts))


def part_for_day(parts: Sequence[DatedPart]) -> ColumnElement[Any]:
    """The index of the part a dive's local day is attributed to, NULL where none covers it.

    Mirrors `tripPartForDay` in opendiving-web's `src/lib/trip-dive-sections.ts`, which places
    the same dives in the same parts on the trip page, so a part's count and its add reach
    exactly the dives its card shows. A part with both dates beats an open-ended one, the
    first such part in the diver's order winning a day two cover; among open-ended parts the
    one whose date is nearer wins, and a tie goes to the first. A part never attributed a day
    - the second of two with the same dates - counts nothing. Copied rather than shared, so a
    change to the rule on either side has to be made on both: `tests/test_trip_candidates.py`
    copies the cases of `trip-dive-sections.test.ts`, and nothing runs across the repos.
    """
    whens: list[tuple[ColumnElement[bool], int]] = [
        (DIVE_LOCAL_DAY.between(part.start_date, part.end_date), index)
        for index, part in enumerate(parts)
        if part.start_date and part.end_date
    ]
    reach: dict[int, ColumnElement[Any]] = {}
    for index, part in enumerate(parts):
        if part.start_date and not part.end_date:
            reach[index] = case((DIVE_LOCAL_DAY >= part.start_date, DIVE_LOCAL_DAY - part.start_date))
        elif part.end_date and not part.start_date:
            reach[index] = case((DIVE_LOCAL_DAY <= part.end_date, literal(part.end_date, Date) - DIVE_LOCAL_DAY))
    if reach:
        # `LEAST` skips NULLs, so the nearest is over the parts that reach the day at all.
        nearest = func.least(*reach.values())
        whens.extend((distance == nearest, index) for index, distance in reach.items())
    if not whens:
        return null()
    return case(*whens)


async def get_dives_page(
    db: AsyncSession,
    *,
    user_id: int,
    offset: int,
    limit: int,
    conditions: Sequence[ColumnElement[bool]] = (),
    sort: DiveListSort = DiveListSort.DATE,
) -> dict[str, Any]:
    """One page of a diver's live dives, in `get_multi`'s `{"data": [...], "total_count": n}`
    shape - rows as plain dicts of every column, so the caller reads the internal ids it
    batches its lookups by. Hand-written for `_LIST_ORDERS`, as `get_certifications_page` is.
    """
    where = (Dive.user_id == user_id, Dive.is_deleted.is_(False), *conditions)
    total_count = await db.scalar(select(func.count()).select_from(Dive).where(*where))
    rows = (
        await db.execute(
            select(*Dive.__table__.columns).where(*where).order_by(*_LIST_ORDERS[sort]).offset(offset).limit(limit)
        )
    ).mappings()
    return {"data": [dict(row) for row in rows], "total_count": total_count or 0}


async def reassign_dives_to_trip(db: AsyncSession, *, user_id: int, from_trip_id: int, to_trip_id: int) -> int:
    """Point every one of a diver's live dives on one trip at another, and return how many
    moved. Does not commit - the caller's delete does, so the two land together.

    Soft-deleted dives are deliberately left behind, and that now costs something it did
    not use to. They are outside everything the diver can see, and the scope originally
    preserved the pairing they were logged with - back when the trip was about to be
    *soft*-deleted and its row survived. It does not preserve anything now: `dive.trip_id`
    is `ON DELETE SET NULL`, and the caller's `DELETE FROM trip` is real, so the cascade
    nulls the column on exactly the dives this `UPDATE` skipped. The promise `erase_trip`
    makes - either the log moved or nothing happened - holds for the log a diver can see
    and not for the rows underneath it.

    Left as a permanent accepted loss rather than fixed, for the reason
    `replace_dive_site_on_dives` gives for the identical case on the site half: no surface
    renders a soft-deleted dive, so there is no visible consequence, and the whole thing
    disappears if dives ever go hard-delete too. The scope also used to have a second
    justification - it kept the returned count equal to the number the web app's
    confirmation dialog had pre-fetched from `GET /dives?trip_uuid=...` - and both that
    count and that dialog are gone. See DECISIONS.md.

    The count itself outlived its route: `erase_trip` discards it now that `DELETE
    /trip/{uuid}` answers a bare `{"message": ...}`. It is kept because it is the natural
    affected-row count of the statement below, and because the database-backed tests assert
    against it.

    `user_id` is redundant against a trip id already resolved for this owner, and is here
    anyway: it is the one condition that cannot be got wrong quietly, since a bulk `UPDATE`
    with a stale or mis-resolved trip id would otherwise rewrite another diver's log.

    `updated_at` is set by hand because `TimestampMixin` gives it no `onupdate`, so every
    writer does - FastCRUD through `DiveUpdateInternal`, and `dive_numbering`'s bulk
    renumber in its own `.values()`. Skipping it here would make "this dive moved to that
    trip" leave a different row behind depending on whether it arrived through this call or
    through `PATCH /dive`, which puts `trip_id` in `update_data` and does bump it - and
    these are the same edit.
    """
    moved = await db.execute(
        update(Dive)
        .where(Dive.trip_id == from_trip_id, Dive.user_id == user_id, Dive.is_deleted.is_(False))
        .values(trip_id=to_trip_id, updated_at=datetime.now(UTC))
        .returning(Dive.id)
    )
    return len(moved.all())


async def assign_candidates_to_trip(
    db: AsyncSession,
    *,
    user_id: int,
    trip_id: int,
    parts: Sequence[DatedPart],
    dive_uuids: Sequence[uuid_pkg.UUID] | None = None,
    part_index: int | None = None,
) -> int:
    """Put the trip's candidates on it - all of them, those among `dive_uuids`, or those
    `part_for_day` attributes to the part at `part_index` - and return how many moved. Does
    not commit.

    Each scope only narrows the candidate predicate, so a dive already on a trip, deleted or
    another diver's is never touched whatever the caller names. `user_id` and `updated_at`
    are here for the reasons `reassign_dives_to_trip` gives.
    """
    narrowing: list[ColumnElement[bool]] = []
    if dive_uuids is not None:
        narrowing.append(Dive.uuid.in_(dive_uuids))
    if part_index is not None:
        narrowing.append(part_for_day(parts) == part_index)
    moved = await db.execute(
        update(Dive)
        .where(Dive.user_id == user_id, Dive.is_deleted.is_(False), candidate_of(parts), *narrowing)
        .values(trip_id=trip_id, updated_at=datetime.now(UTC))
        .returning(Dive.id)
    )
    return len(moved.all())
