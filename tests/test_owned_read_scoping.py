"""Who the read loaders will hand a row to, and which rows come through at all.

Rescued from three files the hard-delete change deleted
(`test_deleted_refs_on_dive_reads.py`, `test_deleted_refs_on_gear_set_reads.py`,
`test_gear_service_history_reachability.py`). Most of what those pinned was the
soft-delete filtering that is now gone, but two things in them survived the behaviour
change untouched and would otherwise have gone unpinned with the rest:

**Cross-user scoping**, on `get_trip_uuids_by_ids` and `_owned_gear_item`. Neither is
reachable through today's callers - both are reached with ids or uuids already resolved
against the caller - which is exactly why they need a test: a guard nothing currently
exercises is a guard a future caller can quietly walk around, and `DECISIONS.md` reasoned
about both explicitly ("`get_trip_uuids_by_ids` also gained a `user_id` scope"; "the two
intentional non-users" of `fetch_owned_or_raise`).

**Archived items still coming through** on the four gear loaders. This mattered when it
was the distinction between two filters; it matters more now that it is the only one left.
Archiving is what retires kit from the dive form's picker while the dives and sets that
already reference it go on showing it, and it is now the sole non-destructive path -
deleting takes the join rows with it. A stray `is_archived` filter on any of these four
would empty the gear list of every diver who tidies up, and break no other test.

The database-backed classes skip themselves when no database is reachable; on a
developer's machine that needs `POSTGRES_SERVER=localhost`. See CONTRIBUTING.md.
"""

import uuid as uuid_pkg
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1.dives import _cached_read_dive, _cached_read_dives
from src.app.api.v1.gear_service import _owned_gear_item
from src.app.core.exceptions.http_exceptions import UnprocessableEntityException
from src.app.crud.crud_dive_gear_items import (
    get_gear_items_for_dive,
    get_gear_items_for_dives,
    replace_gear_items_for_dive,
)
from src.app.crud.crud_gear_set_items import (
    get_gear_items_for_set,
    get_gear_items_for_sets,
    replace_gear_items_for_set,
)
from src.app.crud.crud_trips import get_trip_uuids_by_ids
from src.app.models.user import User
from src.app.schemas.dive import DiveReadInternal
from tests.conftest import db_available
from tests.helpers.generators import create_dive, create_gear_item, create_gear_set, create_trip

# The `@cache` decorator would need Redis and would serve a hit without re-running the
# body, which is the opposite of what these assert. `__wrapped__` is the undecorated
# function.
_read_dive_uncached = cast(Any, _cached_read_dive).__wrapped__
_read_dives_uncached = cast(Any, _cached_read_dives).__wrapped__


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTripUuidLookupScoping:
    @pytest.mark.asyncio
    async def test_another_divers_trip_never_resolves(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        """Not reachable through today's callers, which pass ids read off the caller's own
        dives - this pins the scope so a future caller sourcing ids elsewhere cannot leak
        another logbook's uuid."""
        theirs = create_trip(db, other_diver)

        assert await get_trip_uuids_by_ids(async_db, trip_ids=[theirs.id], user_id=diver.id) == {}

    @pytest.mark.asyncio
    async def test_the_callers_own_trip_resolves(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """The other half, so the test above cannot pass by the lookup being broken."""
        mine = create_trip(db, diver)

        assert await get_trip_uuids_by_ids(async_db, trip_ids=[mine.id], user_id=diver.id) == {mine.id: mine.uuid}

    @pytest.mark.asyncio
    async def test_no_ids_is_no_query(self, async_db: AsyncSession, diver: User) -> None:
        assert await get_trip_uuids_by_ids(async_db, trip_ids=[], user_id=diver.id) == {}


class TestTheDiveReadsScopeThatLookup:
    """The scope above is only worth anything if the routes pass the *owner's* id into it.

    Stubbed rather than database-backed: the question is which kwargs the route builds, and
    a real session would answer it far more slowly and no more exactly.
    """

    @staticmethod
    def _dive_row(trip_id: int | None) -> dict[str, Any]:
        """Built through `DiveReadInternal` rather than as a literal, so a column added to
        the row shape can't leave this fixture one key short of what the route reads."""
        return DiveReadInternal(
            id=5,
            uuid=uuid7(),
            user_id=7,
            trip_id=trip_id,
            dive_number=1,
            start_time=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
            utc_offset_minutes=120,
            duration=1800,
            notes="",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ).model_dump()

    @pytest.mark.asyncio
    async def test_the_single_read_scopes_the_lookup_to_the_owner(self) -> None:
        """The `user_id` the route passes has to be the dive's owner - the same value that
        scopes the cache key - or the scope on the lookup would be decorative."""
        row = self._dive_row(trip_id=11)
        lookup = AsyncMock(return_value={})

        with (
            patch("src.app.api.v1.dives.crud_dives.get", AsyncMock(return_value=row)),
            patch("src.app.api.v1.dives.get_trip_uuids_by_ids", lookup),
            patch("src.app.api.v1.dives.get_mixtures_for_dive", AsyncMock(return_value=[])),
            patch("src.app.api.v1.dives.get_dive_sites_for_dive", AsyncMock(return_value=[])),
            patch("src.app.api.v1.dives.get_gear_items_for_dive", AsyncMock(return_value=[])),
            patch("src.app.api.v1.dives.get_recordings_for_dives", AsyncMock(return_value={})),
        ):
            await _read_dive_uncached(request=None, user_id=7, uuid=row["uuid"], owner_uuid=uuid7(), db=AsyncMock())

        assert lookup.await_args is not None
        assert lookup.await_args.kwargs["user_id"] == 7
        assert lookup.await_args.kwargs["trip_ids"] == [11]

    @pytest.mark.asyncio
    async def test_the_list_path_scopes_the_lookup_too(self) -> None:
        """The bulk shape the `user_id` scope was actually reasoned about: `GET /dives`
        collects `trip_id`s off a page of rows and resolves them in one call, which is the
        caller a future change is most likely to get wrong."""
        rows = [self._dive_row(trip_id=11), self._dive_row(trip_id=None), self._dive_row(trip_id=12)]
        lookup = AsyncMock(return_value={})

        with (
            patch(
                "src.app.api.v1.dives.crud_dives.get_multi",
                AsyncMock(return_value={"data": rows, "total_count": len(rows)}),
            ),
            patch("src.app.api.v1.dives.get_trip_uuids_by_ids", lookup),
            patch("src.app.api.v1.dives.get_dive_sites_for_dives", AsyncMock(return_value={})),
            patch("src.app.api.v1.dives.get_gear_items_for_dives", AsyncMock(return_value={})),
        ):
            await _read_dives_uncached(
                request=None,
                user_id=7,
                user_uuid=uuid7(),
                db=AsyncMock(),
                page=1,
                items_per_page=10,
                trip_id=None,
                course_id=None,
                dive_site_id=None,
                gear_item_id=None,
                species_id=None,
            )

        assert lookup.await_args is not None
        assert lookup.await_args.kwargs["user_id"] == 7
        # Only the rows that have a trip, and the null one filtered out rather than passed
        # through as a `None` the `IN` clause would have to cope with.
        assert lookup.await_args.kwargs["trip_ids"] == [11, 12]


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestOwnedGearItemIsScopedToTheCaller:
    """`_owned_gear_item` is one of the two deliberate non-users of `fetch_owned_or_raise`:
    the uuid is a reference inside a request body rather than the resource being addressed,
    so it answers **422** for both "not yours" and "doesn't exist" rather than 404.
    """

    @pytest.mark.asyncio
    async def test_another_divers_item_is_refused(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        """Answering identically for "not yours" and "doesn't exist" is what keeps someone
        else's gear uuids unprobeable, as the function's docstring says."""
        theirs = create_gear_item(db, other_diver)

        with pytest.raises(UnprocessableEntityException):
            await _owned_gear_item(async_db, theirs.uuid, diver.id)

    @pytest.mark.asyncio
    async def test_an_unknown_uuid_is_refused_the_same_way(self, async_db: AsyncSession, diver: User) -> None:
        with pytest.raises(UnprocessableEntityException):
            await _owned_gear_item(async_db, uuid_pkg.uuid4(), diver.id)


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestArchivedItemsStillComeThrough:
    """Four loaders, one rule, and it is load-bearing on all four.

    Archiving retires kit from the dive form's picker *while* the dives and sets that
    already reference it go on showing it - that is the whole feature, and it is now the
    only way an item leaves the picker without leaving the dives, since deleting takes the
    join rows with it. A filter added on `is_archived` here would break that silently.
    """

    @pytest.mark.asyncio
    async def test_on_a_dive(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        archived = create_gear_item(db, diver, is_archived=True)
        dive = create_dive(db, diver)
        await replace_gear_items_for_dive(async_db, dive_id=dive.id, gear_item_ids=[archived.id])

        items = await get_gear_items_for_dive(async_db, dive_id=dive.id)

        assert [(item.uuid, item.is_archived) for item in items] == [(archived.uuid, True)]

    @pytest.mark.asyncio
    async def test_on_the_batched_dive_loader(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """`GET /dives` builds its page through this one, so the single-dive loader alone
        would leave the list page disagreeing with the detail page."""
        archived = create_gear_item(db, diver, is_archived=True)
        dive = create_dive(db, diver)
        await replace_gear_items_for_dive(async_db, dive_id=dive.id, gear_item_ids=[archived.id])

        by_dive = await get_gear_items_for_dives(async_db, dive_ids=[dive.id])

        assert [item.uuid for item in by_dive[dive.id]] == [archived.uuid]

    @pytest.mark.asyncio
    async def test_on_a_set(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """It matters more on a set than on a dive: a set that quietly shed its archived
        members would stop loading the configuration it names."""
        archived = create_gear_item(db, diver, is_archived=True)
        gear_set = create_gear_set(db, diver)
        await replace_gear_items_for_set(async_db, gear_set_id=gear_set.id, gear_item_ids=[archived.id])

        items = await get_gear_items_for_set(async_db, gear_set_id=gear_set.id)

        assert [(item.uuid, item.is_archived) for item in items] == [(archived.uuid, True)]

    @pytest.mark.asyncio
    async def test_on_the_batched_set_loader(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """And `GET /gear-sets` builds its page through this one. Also pins the pre-seeded
        empty list, which is what keeps a set with no members from dropping out of the
        mapping its caller indexes."""
        archived = create_gear_item(db, diver, is_archived=True)
        stocked, empty = create_gear_set(db, diver), create_gear_set(db, diver)
        await replace_gear_items_for_set(async_db, gear_set_id=stocked.id, gear_item_ids=[archived.id])

        by_set = await get_gear_items_for_sets(async_db, gear_set_ids=[stocked.id, empty.id])

        assert [item.uuid for item in by_set[stocked.id]] == [archived.uuid]
        assert by_set[empty.id] == []
