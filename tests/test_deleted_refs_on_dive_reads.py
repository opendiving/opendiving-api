"""Tests that a dive read stops naming a dive site, trip or gear item the diver has deleted.

Every one of those loaders keeps its links when the thing they point at is soft-deleted -
that is what lets an export read the record back - so nothing but the query's own `WHERE`
decides whether the app renders an orphan. A stubbed session answers with whatever rows the
stub was handed, which is exactly the question here, so these run against a live Postgres
and skip themselves otherwise. See CONTRIBUTING.md for why a run on the host needs
`POSTGRES_SERVER=localhost` to make them execute.

The route-level halves - the erase routes dropping the dive caches so a cached read cannot
outlive the filter - live against stubs that can see the invalidation call:
`test_move_dives_on_delete.py` for `erase_trip`/`erase_dive_site`, and
`TestErasingGearItemDropsTheDiveCaches` at the bottom of this module for `erase_gear_item`,
which has no such module of its own. The serialized shape the clients consume is pinned here
too, against a stub, since that one is about `_to_public_dive` and a schema default rather
than a query.
"""

import uuid as uuid_pkg
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

import src.app.api.v1.gear_items as gear_items_module
from src.app.api.v1.dives import _cached_read_dive, _cached_read_dives
from src.app.crud.crud_dive_dive_sites import (
    get_dive_sites_for_dive,
    get_dive_sites_for_dives,
    replace_dive_sites_for_dive,
)
from src.app.crud.crud_dive_gear_items import (
    get_gear_items_for_dive,
    get_gear_items_for_dives,
    replace_gear_items_for_dive,
)
from src.app.crud.crud_dive_sites import crud_dive_sites
from src.app.crud.crud_gear_items import crud_gear_items
from src.app.crud.crud_trips import crud_trips, get_trip_uuids_by_ids
from src.app.models.dive import Dive
from src.app.models.dive_dive_site import DiveDiveSite
from src.app.models.dive_gear_item import DiveGearItem
from src.app.models.user import User
from src.app.schemas.dive import DiveReadInternal
from src.app.schemas.gear_item import GearItemReadInternal, GearType
from tests.conftest import db_available
from tests.helpers.generators import create_dive, create_dive_site, create_gear_item, create_trip

# The undecorated body. `_cached_read_dive` carries `@cache`, which would need a Redis
# client in place and would then serialize the response on the way out - neither of which
# is the question here, since the mapping under test happens inside the body. `cast`
# because the decorator's return type does not advertise `__wrapped__`.
_read_dive_uncached = cast(Any, _cached_read_dive).__wrapped__
_read_dives_uncached = cast(Any, _cached_read_dives).__wrapped__


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestDeletedDiveSitesAreNotRendered:
    @pytest.mark.asyncio
    async def test_a_deleted_site_drops_off_the_dive(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """The bug in one assertion: `GET /dive-site/{uuid}` 404s for this site, so the
        dive page must not go on showing it."""
        live, deleted = create_dive_site(db, diver), create_dive_site(db, diver, is_deleted=True)
        dive = create_dive(db, diver)
        await replace_dive_sites_for_dive(async_db, dive_id=dive.id, dive_site_ids=[live.id, deleted.id])

        sites = await get_dive_sites_for_dive(async_db, dive_id=dive.id)

        assert [site.uuid for site in sites] == [live.uuid]

    @pytest.mark.asyncio
    async def test_a_dive_whose_only_site_is_deleted_reads_back_empty(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        deleted = create_dive_site(db, diver, is_deleted=True)
        dive = create_dive(db, diver)
        await replace_dive_sites_for_dive(async_db, dive_id=dive.id, dive_site_ids=[deleted.id])

        assert await get_dive_sites_for_dive(async_db, dive_id=dive.id) == []

    @pytest.mark.asyncio
    async def test_the_next_site_inherits_the_primary_slot(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """Position 0 is the primary site every single-site surface shows. Deleting the
        primary promotes the one behind it rather than leaving the dive headed by a site
        that no longer exists - `position` is a sort key, not an identity."""
        deleted, second = create_dive_site(db, diver, is_deleted=True), create_dive_site(db, diver)
        dive = create_dive(db, diver)
        await replace_dive_sites_for_dive(async_db, dive_id=dive.id, dive_site_ids=[deleted.id, second.id])

        sites = await get_dive_sites_for_dive(async_db, dive_id=dive.id)

        assert [site.uuid for site in sites] == [second.uuid]

    @pytest.mark.asyncio
    async def test_the_batched_loader_hides_them_too(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """`GET /dives` enriches its rows through the batched loader, so a filter on the
        single-dive one alone would leave the list page still showing the deleted site."""
        live, deleted = create_dive_site(db, diver), create_dive_site(db, diver, is_deleted=True)
        with_live, with_deleted = create_dive(db, diver), create_dive(db, diver)
        await replace_dive_sites_for_dive(async_db, dive_id=with_live.id, dive_site_ids=[live.id])
        await replace_dive_sites_for_dive(async_db, dive_id=with_deleted.id, dive_site_ids=[deleted.id])

        by_dive = await get_dive_sites_for_dives(async_db, dive_ids=[with_live.id, with_deleted.id])

        assert [site.uuid for site in by_dive[with_live.id]] == [live.uuid]
        # Present and empty rather than absent - the pre-seeded lists are what keep a dive
        # whose every site is gone from dropping out of the mapping its caller indexes.
        assert by_dive[with_deleted.id] == []


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestDeletedGearItemsAreNotRendered:
    @pytest.mark.asyncio
    async def test_a_deleted_item_drops_off_the_dive(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """The same assertion as the dive-site case: `GET /gear-item/{uuid}` 404s for this
        item, so the dive page must not go on listing it."""
        live, deleted = create_gear_item(db, diver), create_gear_item(db, diver, is_deleted=True)
        dive = create_dive(db, diver)
        await replace_gear_items_for_dive(async_db, dive_id=dive.id, gear_item_ids=[live.id, deleted.id])

        items = await get_gear_items_for_dive(async_db, dive_id=dive.id)

        assert [item.uuid for item in items] == [live.uuid]

    @pytest.mark.asyncio
    async def test_a_dive_whose_only_item_is_deleted_reads_back_empty(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        deleted = create_gear_item(db, diver, is_deleted=True)
        dive = create_dive(db, diver)
        await replace_gear_items_for_dive(async_db, dive_id=dive.id, gear_item_ids=[deleted.id])

        assert await get_gear_items_for_dive(async_db, dive_id=dive.id) == []

    @pytest.mark.asyncio
    async def test_an_archived_item_still_comes_through(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """The distinction the filter has to keep. Archiving retires kit from the dive
        form's picker *so that* the dives that used it go on showing it - a filter on
        `is_archived` as well would quietly empty the gear list of every diver who tidies
        up. It comes back flagged, for the client to render as it likes."""
        archived = create_gear_item(db, diver, is_archived=True)
        dive = create_dive(db, diver)
        await replace_gear_items_for_dive(async_db, dive_id=dive.id, gear_item_ids=[archived.id])

        items = await get_gear_items_for_dive(async_db, dive_id=dive.id)

        assert [(item.uuid, item.is_archived) for item in items] == [(archived.uuid, True)]

    @pytest.mark.asyncio
    async def test_the_batched_loader_hides_them_too(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """`GET /dives` enriches its rows through the batched loader, so a filter on the
        single-dive one alone would leave the list page still listing the deleted item."""
        live, deleted = create_gear_item(db, diver), create_gear_item(db, diver, is_deleted=True)
        with_live, with_deleted = create_dive(db, diver), create_dive(db, diver)
        await replace_gear_items_for_dive(async_db, dive_id=with_live.id, gear_item_ids=[live.id])
        await replace_gear_items_for_dive(async_db, dive_id=with_deleted.id, gear_item_ids=[deleted.id])

        by_dive = await get_gear_items_for_dives(async_db, dive_ids=[with_live.id, with_deleted.id])

        assert [item.uuid for item in by_dive[with_live.id]] == [live.uuid]
        # Present and empty rather than absent, exactly as for sites - the pre-seeded lists
        # are what keep a dive whose every item is gone from dropping out of the mapping.
        assert by_dive[with_deleted.id] == []


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestDeletedTripsAreNotRendered:
    @pytest.mark.asyncio
    async def test_a_deleted_trip_stops_resolving(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """A dive keeps its `trip_id` when the trip is deleted, so the miss here is what
        turns into the `trip_uuid: null` the dive reads answer with."""
        live, deleted = create_trip(db, diver), create_trip(db, diver, is_deleted=True)

        by_id = await get_trip_uuids_by_ids(async_db, trip_ids=[live.id, deleted.id], user_id=diver.id)

        assert by_id == {live.id: live.uuid}

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
    async def test_no_ids_is_no_query(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        assert await get_trip_uuids_by_ids(async_db, trip_ids=[], user_id=diver.id) == {}


class TestTheSerializedDiveStillCarriesTheKey:
    """What the clients actually consume, which neither loader test states.

    `trip_uuid` has to come back **present and null** rather than omitted: the resolution
    is a `.get()` miss in `_cached_read_dive` and a `default=None` on `DiveRead`, two facts
    in two files that a refactor could change independently without any query breaking.
    Stubbed rather than database-backed, since the question is the mapping and the schema
    default.
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
    async def test_a_dive_whose_trip_was_deleted_serializes_trip_uuid_as_null(self) -> None:
        owner_uuid = uuid7()
        row = self._dive_row(trip_id=11)

        with (
            patch("src.app.api.v1.dives.crud_dives.get", AsyncMock(return_value=row)),
            # The deleted trip resolves to nothing - an empty mapping is exactly what the
            # filtered lookup returns for a `trip_id` whose trip is gone.
            patch("src.app.api.v1.dives.get_trip_uuids_by_ids", AsyncMock(return_value={})),
            patch("src.app.api.v1.dives.get_mixtures_for_dive", AsyncMock(return_value=[])),
            patch("src.app.api.v1.dives.get_dive_sites_for_dive", AsyncMock(return_value=[])),
            patch("src.app.api.v1.dives.get_gear_items_for_dive", AsyncMock(return_value=[])),
            patch("src.app.api.v1.dives.get_file_infos_for_dives", AsyncMock(return_value={})),
            patch("src.app.api.v1.dives.get_profile_infos_for_dives", AsyncMock(return_value={})),
        ):
            dive = await _read_dive_uncached(
                request=None, user_id=7, uuid=row["uuid"], owner_uuid=owner_uuid, db=AsyncMock()
            )

        serialized = dive.model_dump()
        assert "trip_uuid" in serialized
        assert serialized["trip_uuid"] is None
        assert serialized["dive_sites"] == []

    @pytest.mark.asyncio
    async def test_a_live_trip_still_comes_through(self) -> None:
        owner_uuid, trip_uuid = uuid7(), uuid7()
        row = self._dive_row(trip_id=11)

        with (
            patch("src.app.api.v1.dives.crud_dives.get", AsyncMock(return_value=row)),
            patch("src.app.api.v1.dives.get_trip_uuids_by_ids", AsyncMock(return_value={11: trip_uuid})),
            patch("src.app.api.v1.dives.get_mixtures_for_dive", AsyncMock(return_value=[])),
            patch("src.app.api.v1.dives.get_dive_sites_for_dive", AsyncMock(return_value=[])),
            patch("src.app.api.v1.dives.get_gear_items_for_dive", AsyncMock(return_value=[])),
            patch("src.app.api.v1.dives.get_file_infos_for_dives", AsyncMock(return_value={})),
            patch("src.app.api.v1.dives.get_profile_infos_for_dives", AsyncMock(return_value={})),
        ):
            dive = await _read_dive_uncached(
                request=None, user_id=7, uuid=row["uuid"], owner_uuid=owner_uuid, db=AsyncMock()
            )

        assert dive.trip_uuid == trip_uuid

    @pytest.mark.asyncio
    async def test_the_lookup_is_scoped_to_the_reader(self) -> None:
        """The `user_id` the route passes has to be the dive's owner - the same value that
        scopes the cache key - or the scope added to the lookup would be decorative."""
        row = self._dive_row(trip_id=11)
        lookup = AsyncMock(return_value={})

        with (
            patch("src.app.api.v1.dives.crud_dives.get", AsyncMock(return_value=row)),
            patch("src.app.api.v1.dives.get_trip_uuids_by_ids", lookup),
            patch("src.app.api.v1.dives.get_mixtures_for_dive", AsyncMock(return_value=[])),
            patch("src.app.api.v1.dives.get_dive_sites_for_dive", AsyncMock(return_value=[])),
            patch("src.app.api.v1.dives.get_gear_items_for_dive", AsyncMock(return_value=[])),
            patch("src.app.api.v1.dives.get_file_infos_for_dives", AsyncMock(return_value={})),
            patch("src.app.api.v1.dives.get_profile_infos_for_dives", AsyncMock(return_value={})),
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
                dive_site_id=None,
                gear_item_id=None,
            )

        assert lookup.await_args is not None
        assert lookup.await_args.kwargs["user_id"] == 7
        # Only the rows that have a trip, and the null one filtered out rather than passed
        # through as a `None` the `IN` clause would have to cope with.
        assert lookup.await_args.kwargs["trip_ids"] == [11, 12]


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestAPlainDeleteLeavesTheLinksAlone:
    """The invariant export depends on, which nothing else pins.

    Both routes soft-delete and touch no join rows, which is what leaves `_owned` something
    to resurrect. The tests above build `is_deleted=True` rows directly and the route tests
    stub the CRUD delete, so a change that started clearing the links on delete - the
    "option 4" DECISIONS.md rejects - would pass the entire suite while quietly emptying
    `still_referenced`.
    """

    @pytest.mark.asyncio
    async def test_deleting_a_site_keeps_its_dive_links(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        site = create_dive_site(db, diver)
        dive = create_dive(db, diver)
        await replace_dive_sites_for_dive(async_db, dive_id=dive.id, dive_site_ids=[site.id])

        await crud_dive_sites.delete(db=async_db, uuid=site.uuid)

        links = await async_db.execute(select(DiveDiveSite.dive_site_id).where(DiveDiveSite.dive_id == dive.id))
        assert [row.dive_site_id for row in links] == [site.id]
        # And the read hides it, so the row surviving is not the read being unfiltered.
        assert await get_dive_sites_for_dive(async_db, dive_id=dive.id) == []

    @pytest.mark.asyncio
    async def test_deleting_a_trip_keeps_its_dives_pointing_at_it(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        trip = create_trip(db, diver)
        dive = create_dive(db, diver, trip=trip)

        await crud_trips.delete(db=async_db, uuid=trip.uuid)

        stored = await async_db.execute(select(Dive.trip_id).where(Dive.id == dive.id))
        assert stored.scalar_one() == trip.id
        assert await get_trip_uuids_by_ids(async_db, trip_ids=[trip.id], user_id=diver.id) == {}

    @pytest.mark.asyncio
    async def test_deleting_a_gear_item_keeps_its_dive_links(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        item = create_gear_item(db, diver)
        dive = create_dive(db, diver)
        await replace_gear_items_for_dive(async_db, dive_id=dive.id, gear_item_ids=[item.id])

        await crud_gear_items.delete(db=async_db, uuid=item.uuid)

        links = await async_db.execute(select(DiveGearItem.gear_item_id).where(DiveGearItem.dive_id == dive.id))
        assert [row.gear_item_id for row in links] == [item.id]
        # And the read hides it, so the row surviving is not the read being unfiltered.
        assert await get_gear_items_for_dive(async_db, dive_id=dive.id) == []


class TestErasingGearItemDropsTheDiveCaches:
    """The route half of the gear filter, stubbed - the invalidation is not a query.

    `erase_gear_item` already invalidated unconditionally before the filter landed, so
    unlike `erase_trip` it needed no change. That makes it exactly the kind of thing a
    later cleanup removes as redundant: the call has no visible effect on the delete
    itself, and what it protects lives in another file. It is what stops a cached dive
    read going on listing kit a fresh read now omits, for the rest of the hour.
    """

    @staticmethod
    def _stub_route(monkeypatch: pytest.MonkeyPatch) -> tuple[uuid_pkg.UUID, AsyncMock]:
        uuid = uuid7()
        item = GearItemReadInternal(
            id=3,
            uuid=uuid,
            user_id=7,
            name="MK25 EVO",
            brand="Scubapro",
            type=GearType.REGULATOR,
            notes="",
            rented=False,
            is_archived=False,
            archived_at=None,
            dive_count=4,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        invalidate_dives = AsyncMock()

        monkeypatch.setattr(gear_items_module, "_get_owned_gear_item", AsyncMock(return_value=item))
        monkeypatch.setattr(gear_items_module, "soft_delete_schedules_for_gear_item", AsyncMock())
        monkeypatch.setattr(gear_items_module.crud_gear_items, "delete", AsyncMock())
        monkeypatch.setattr(gear_items_module, "invalidate_gear_caches", AsyncMock())
        monkeypatch.setattr(gear_items_module, "invalidate_dive_caches", invalidate_dives)

        return uuid, invalidate_dives

    @pytest.mark.asyncio
    async def test_the_owners_dive_caches_are_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        uuid, invalidate_dives = self._stub_route(monkeypatch)

        await gear_items_module.erase_gear_item(
            request=MagicMock(),
            uuid=uuid,
            current_user={"id": 7, "uuid": uuid7()},
            db=MagicMock(),
        )

        # The *item owner's* id, not the caller's - they are the same today only because
        # someone else's item reads as a 404 before this point.
        invalidate_dives.assert_awaited_once_with(7)
