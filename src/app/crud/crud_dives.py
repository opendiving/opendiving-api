from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from fastcrud import FastCRUD
from sqlalchemy import ARRAY, Boolean, ColumnElement, Integer, func, select, update
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
    the single place that conversion happens, and the failure mode of a second copy of it in SQL
    is a list that quietly disagrees with the dive pages it was built from.

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
