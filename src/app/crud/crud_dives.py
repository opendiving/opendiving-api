from datetime import UTC, datetime

from fastcrud import FastCRUD
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.dive import Dive
from ..models.dive_dive_site import DiveDiveSite
from ..models.dive_gear_item import DiveGearItem
from ..schemas.dive import DiveCreateInternal, DiveDelete, DiveReadInternal, DiveUpdate, DiveUpdateInternal

CRUDDive = FastCRUD[Dive, DiveCreateInternal, DiveUpdate, DiveUpdateInternal, DiveDelete, DiveReadInternal]

# Lets callers filter dives by dive site or gear item (e.g. `id__at_dive_site=some_id`,
# `id__with_gear_item=some_id`) with a single `IN (subquery)` condition instead of
# resolving matching dive ids in a separate round trip.
crud_dives = CRUDDive(
    Dive,
    custom_filters={
        "at_dive_site": lambda column: (
            lambda dive_site_id: column.in_(
                select(DiveDiveSite.dive_id).where(DiveDiveSite.dive_site_id == dive_site_id)
            )
        ),
        "with_gear_item": lambda column: (
            lambda gear_item_id: column.in_(
                select(DiveGearItem.dive_id).where(DiveGearItem.gear_item_id == gear_item_id)
            )
        ),
    },
)


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
